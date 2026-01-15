import re
import time
from datetime import datetime, timezone
import httpx
import asyncio
import os
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.append('.')
from user_info import User_info
from csv_gen import csv_gen
from md_gen import md_gen
from cache_gen import cache_gen
from url_utils import quote_url, cookie_get, require_cookie_fields
from rich_output import JsonlWriter, extract_tweet_record, unwrap_tweet_result
from crawl_state import build_run_key, load_state, save_state, clear_state, infer_existing_media_count

def _strip_jsonc_comments(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    in_string = False
    string_quote = '"'
    escape = False
    in_line_comment = False
    in_block_comment = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ''

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
                out.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == '*' and nxt == '/':
                in_block_comment = False
                i += 2
                continue
            if ch == '\n':
                out.append(ch)
            i += 1
            continue

        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == string_quote:
                in_string = False
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            string_quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == '/' and nxt == '/':
            in_line_comment = True
            i += 2
            continue

        if ch == '/' and nxt == '*':
            in_block_comment = True
            i += 2
            continue

        out.append(ch)
        i += 1

    return ''.join(out)

def load_settings(path: str = 'settings.json') -> dict:
    with open(path, 'r', encoding='utf8') as f:
        raw = f.read()
    settings = json.loads(_strip_jsonc_comments(raw))

    # Optional local overrides (e.g. keep secrets like cookie out of git)
    local_path = Path(path).with_name('settings.local.json')
    if local_path.exists():
        with open(local_path, 'r', encoding='utf8') as f:
            local_raw = f.read()
        local_settings = json.loads(_strip_jsonc_comments(local_raw))
        if isinstance(local_settings, dict):
            settings.update(local_settings)

    return settings

def del_special_char(string):
    string = re.sub(r'[^\u4e00-\u9fa5\u0030-\u0039\u0041-\u005a\u0061-\u007a\u3040-\u31FF\.]', '', string)
    return string

def stamp2time(msecs_stamp:int) -> str:
    timeArray = time.localtime(msecs_stamp/1000)
    otherStyleTime = time.strftime("%Y-%m-%d %H-%M", timeArray)
    return otherStyleTime

def time2stamp(timestr:str) -> int:
    datetime_obj = datetime.strptime(timestr, "%Y-%m-%d")
    msecs_stamp = int(time.mktime(datetime_obj.timetuple()) * 1000.0 + datetime_obj.microsecond / 1000.0)
    return msecs_stamp

def time_comparison(now, start, end):
    start_label = True
    start_down  = False
    #twitter : latest -> old
    if now >= start and now <= end:     #符合时间条件，下载
        start_down = True
    elif now < start:     #超出时间范围，结束
        start_label = False
    return [start_down, start_label]
    

#读取配置
log_output = False
has_retweet = False
has_highlights = False
has_likes = False
has_video = False
download_media = True
csv_file = None
cache_data = None
down_log = False
autoSync = False

md_file = None
md_output = True
media_count_limit = 0
rich_output = True
rich_include_raw_legacy = False
rich_writer = None
rich_seen_tweet_ids = set()

start_time_stamp = 655028357000   #1990-10-04
end_time_stamp = 2548484357000    #2050-10-04
start_label = True
First_Page = True       #首页提取内容时特殊处理

settings = load_settings('settings.json')
if not settings['save_path']:
    settings['save_path'] = os.getcwd()
settings['save_path'] += os.sep
if settings['has_retweet']:
    has_retweet = True
if settings['high_lights']:
    has_highlights = True
    has_retweet = False
if settings['time_range']:
    time_range = True
    start_time,end_time = settings['time_range'].split(':')
    start_time_stamp,end_time_stamp = time2stamp(start_time),time2stamp(end_time)
if settings['autoSync']:
    autoSync = True
if settings['down_log']:
    down_log = True
if settings['likes']:   #likes的逻辑和retweet大致相同
    has_retweet = True
    has_likes = True
    has_highlights = False
    start_time_stamp = 655028357000   #1990-10-04
    end_time_stamp = 2548484357000    #2050-10-04
if settings['has_video']:
    has_video = True
download_media = bool(settings.get('download_media', True))
if settings['log_output']:
    log_output = True
if settings['max_concurrent_requests']:
    max_concurrent_requests = settings['max_concurrent_requests']
else:
    max_concurrent_requests = 8
###### proxy ######
if settings['proxy']:
    proxies = settings['proxy']
else:
    proxies = None
rich_output = bool(settings.get('rich_output', True))
rich_include_raw_legacy = bool(settings.get('rich_include_raw_legacy', False))

############
if settings['image_format'] == 'orig':
    orig_format = True
    img_format = 'jpg'
else:
    orig_format = False
    img_format = settings['image_format']

if not settings['md_output']:
    md_output = False

if settings['media_count_limit']:
    media_count_limit = settings['media_count_limit']

backup_stamp = start_time_stamp

_headers = {
    'user-agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
    'authorization':'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
}
_headers['cookie'] = settings['cookie']

request_count = 0    #请求次数计数
down_count = 0      #下载图片数计数


class RateLimitExceeded(RuntimeError):
    def __init__(self, message: str = 'Rate limit exceeded', *, reset_at: Optional[int] = None, retry_after: Optional[int] = None):
        super().__init__(message)
        self.reset_at = reset_at
        self.retry_after = retry_after


def _try_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _rate_limit_reset_from_headers(headers: httpx.Headers) -> Tuple[Optional[int], Optional[int]]:
    reset_at = _try_int(headers.get('x-rate-limit-reset'))
    retry_after = _try_int(headers.get('retry-after'))
    return reset_at, retry_after


RUN_KEY = build_run_key(
    time_range=str(settings.get('time_range', '') or ''),
    has_retweet=has_retweet,
    has_highlights=has_highlights,
    has_likes=has_likes,
)


def _ensure_csrf_headers(headers: dict) -> bool:
    cookie = str(headers.get('cookie', '')).strip()
    try:
        require_cookie_fields(cookie, 'auth_token', 'ct0')
    except Exception as e:
        print('settings.json 的 cookie 需要至少包含 auth_token 与 ct0')
        print(e)
        return False
    headers['x-csrf-token'] = cookie_get(cookie, 'ct0')
    return True


def _print_api_errors(raw: object) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    errors = raw.get('errors')
    if not errors:
        return None
    first = errors[0] if isinstance(errors, list) and errors else errors
    code = first.get('code') if isinstance(first, dict) else None
    msg = first.get('message') if isinstance(first, dict) else str(first)
    print(f'API错误: {code} {msg}')
    lower = str(msg).lower()
    if code == 353 or 'csrf' in lower:
        print('提示: 需要 cookie 中的 ct0 与请求头 x-csrf-token 匹配；检查 cookie 是否包含 ct0，且未复制错。')
        return 'csrf'
    elif 'authenticate' in lower or 'login' in lower or 'unauthorized' in lower:
        print('提示: 可能是 auth_token 已失效/被退出登录，请更新 cookie。')
        return 'auth'
    elif 'rate limit' in lower:
        print('提示: API次数已超限，换号/等隔天或减少请求量。')
        return 'rate_limit'
    return 'other'

def get_other_info(_user_info):
    url = 'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={"screen_name":"' + _user_info.screen_name + '","withSafetyModeUserFields":false}&features={"hidden_profile_likes_enabled":false,"hidden_profile_subscriptions_enabled":false,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"subscriptions_verification_info_verified_since_enabled":true,"highlights_tweets_tab_ui_enabled":true,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true}&fieldToggles={"withAuxiliaryUserLabels":false}'
    response = ''
    try:
        if 'x-csrf-token' not in _headers and not _ensure_csrf_headers(_headers):
            return False
        global request_count
        resp = httpx.get(quote_url(url), headers=_headers, proxy=proxies)
        response = resp.text
        request_count += 1
        if resp.status_code == 429:
            reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
            raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
        try:
            raw_data = json.loads(response)
        except Exception:
            if 'rate limit exceeded' in str(response).lower():
                reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
                raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
            raise
        err_kind = _print_api_errors(raw_data)
        if err_kind:
            print(response)
            if err_kind == 'rate_limit':
                reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
                raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
            return False
        _user_info.rest_id = raw_data['data']['user']['result']['rest_id']
        _user_info.name = raw_data['data']['user']['result']['legacy']['name']
        _user_info.statuses_count = raw_data['data']['user']['result']['legacy']['statuses_count']
        _user_info.media_count = raw_data['data']['user']['result']['legacy']['media_count']
    except RateLimitExceeded:
        raise
    except Exception as e:
        print('获取信息失败')
        print(e)
        print(response)
        return False
    return True

def print_info(_user_info):
    print(
        f'''
        <======基本信息=====>
        昵称:{_user_info.name.encode('utf-8', errors='replace').decode('utf-8')}
        用户名:{_user_info.screen_name}
        数字ID:{_user_info.rest_id}
        总推数(含转推):{_user_info.statuses_count}
        含图片/视频/音频推数(不含转推):{_user_info.media_count}
        <==================>
        开始爬取...
        '''
    )

def get_download_url(_user_info):
    response = ''

    def get_heighest_video_quality(variants) -> str:   #找到最高质量的视频地址,并返回

        if len(variants) == 1:      #gif适配
            return variants[0]['url']
        
        max_bitrate = 0
        heighest_url = None
        for i in variants:
            if 'bitrate' in i:
                if int(i['bitrate']) > max_bitrate:
                    max_bitrate = int(i['bitrate'])
                    heighest_url = i['url']
        return heighest_url


    def get_url_from_content(content):
        global start_label
        _photo_lst = []
        if has_retweet or has_highlights:
            x_label = 'content'
        else:
            x_label = 'item'
        for i in content:
            try:
                if 'promoted-tweet' in i['entryId']:        #排除广告
                    continue
                if 'tweet' in i['entryId']:     #正常推文
                    tweet_result = i[x_label]['itemContent']['tweet_results']['result']
                    tweet_node = unwrap_tweet_result(tweet_result)
                    legacy = tweet_node.get('legacy') if isinstance(tweet_node, dict) else None
                    if not isinstance(legacy, dict):
                        continue

                    frr = [legacy.get('favorite_count'), legacy.get('retweet_count'), legacy.get('reply_count')]
                    try:
                        editable_until = int(tweet_node['edit_control']['editable_until_msecs'])
                    except Exception:
                        editable_until = int(tweet_node['edit_control']['edit_control_initial']['editable_until_msecs'])
                    tweet_msecs = editable_until - 3600000
                    timestr = stamp2time(tweet_msecs)

                    #我知道这边代码很烂
                    #但我实在不想重构 ( º﹃º )

                    _result = time_comparison(tweet_msecs, start_time_stamp, end_time_stamp)
                    if _result[0]:  #符合时间限制
                        if 'retweeted_status_result' not in legacy: #判断是否为转推,以及是否获取转推
                            name = _user_info.name
                            screen_name = _user_info.screen_name
                            if has_likes:
                                a2 = tweet_node.get('core', {}).get('user_results', {}).get('result', {}).get('legacy', {})
                                name = a2.get('name') or name
                                screen_name = a2.get('screen_name') or screen_name

                            if rich_output and rich_writer:
                                rec = extract_tweet_record(
                                    tweet_node,
                                    url_fallback_screen_name=screen_name,
                                    editable_until_msecs=editable_until,
                                    context={"timeline": "likes" if has_likes else ("highlights" if has_highlights else ("tweets" if has_retweet else "media"))},
                                    include_raw_legacy=rich_include_raw_legacy,
                                )
                                if rec and rec.get("tweet_id") and rec["tweet_id"] not in rich_seen_tweet_ids:
                                    rich_seen_tweet_ids.add(rec["tweet_id"])
                                    rich_writer.write(rec)

                            if 'extended_entities' in legacy:
                                tweet_id = legacy.get("id_str") or tweet_node.get("rest_id")
                                tweet_url = f"https://x.com/{screen_name}/status/{tweet_id}" if tweet_id else None
                                for _media in legacy['extended_entities']['media']:
                                    if not isinstance(_media, dict):
                                        continue
                                    if 'video_info' in _media:
                                        if not has_video:
                                            continue
                                        media_url = get_heighest_video_quality(_media['video_info']['variants'])
                                        prefix = f'{timestr}-vid'
                                        media_type = 'Video'
                                    else:
                                        media_url = _media.get('media_url_https')
                                        prefix = f'{timestr}-img'
                                        media_type = 'Image'
                                    if not media_url:
                                        continue
                                    csv_info = [tweet_msecs, name, f'@{screen_name}', tweet_url, media_type, media_url, '', legacy.get('full_text', '')] + frr
                                    media_meta = {
                                        "tweet_id": tweet_id,
                                        "tweet_url": tweet_url,
                                        "created_at_ms": tweet_msecs,
                                        "media": _media,
                                    }
                                    _photo_lst.append((media_url, prefix, csv_info, media_meta))

                        elif has_retweet:
                            rt_node = unwrap_tweet_result(legacy['retweeted_status_result']['result'])
                            rt_legacy = rt_node.get('legacy') if isinstance(rt_node, dict) else None
                            if not isinstance(rt_legacy, dict):
                                continue
                            name = rt_node.get('core', {}).get('user_results', {}).get('result', {}).get('legacy', {}).get('name')
                            screen_name = rt_node.get('core', {}).get('user_results', {}).get('result', {}).get('legacy', {}).get('screen_name')
                            full_text = rt_legacy.get('full_text', '')
                            id_str = rt_legacy.get('id_str')
                            
                            if rich_output and rich_writer:
                                rec = extract_tweet_record(
                                    rt_node,
                                    url_fallback_screen_name=screen_name,
                                    editable_until_msecs=editable_until,
                                    context={"timeline": "retweets", "retweeted_by": {"screen_name": _user_info.screen_name, "name": _user_info.name}},
                                    include_raw_legacy=rich_include_raw_legacy,
                                )
                                if rec and rec.get("tweet_id") and rec["tweet_id"] not in rich_seen_tweet_ids:
                                    rich_seen_tweet_ids.add(rec["tweet_id"])
                                    rich_writer.write(rec)

                            if 'extended_entities' in rt_legacy and screen_name != _user_info.screen_name:
                                tweet_url = f"https://x.com/{screen_name}/status/{id_str}" if id_str else None
                                for _media in rt_legacy['extended_entities']['media']:
                                    if not isinstance(_media, dict):
                                        continue
                                    if 'video_info' in _media:
                                        if not has_video:
                                            continue
                                        media_url = get_heighest_video_quality(_media['video_info']['variants'])
                                        prefix = f'{timestr}-vid-retweet'
                                        media_type = 'Video'
                                    else:
                                        media_url = _media.get('media_url_https')
                                        prefix = f'{timestr}-img-retweet'
                                        media_type = 'Image'
                                    if not media_url:
                                        continue
                                    csv_info = [tweet_msecs, name, f"@{screen_name}", tweet_url, media_type, media_url, '', full_text] + frr
                                    media_meta = {
                                        "tweet_id": id_str or rt_node.get("rest_id"),
                                        "tweet_url": tweet_url,
                                        "created_at_ms": tweet_msecs,
                                        "media": _media,
                                        "context": {"retweeted_by": {"screen_name": _user_info.screen_name, "name": _user_info.name}},
                                    }
                                    _photo_lst.append((media_url, prefix, csv_info, media_meta))

                    elif not _result[1]:    #已超出目标时间范围
                        start_label = False
                        break
                
                elif 'profile-conversation' in i['entryId']:    #回复的推文(对话线索)
                    tweet_result = i[x_label]['items'][0]['item']['itemContent']['tweet_results']['result']
                    tweet_node = unwrap_tweet_result(tweet_result)
                    legacy = tweet_node.get('legacy') if isinstance(tweet_node, dict) else None
                    if not isinstance(legacy, dict):
                        continue
                    frr = [legacy.get('favorite_count'), legacy.get('retweet_count'), legacy.get('reply_count')]
                    try:
                        editable_until = int(tweet_node['edit_control']['editable_until_msecs'])
                    except Exception:
                        editable_until = int(tweet_node['edit_control']['edit_control_initial']['editable_until_msecs'])
                    tweet_msecs = editable_until - 3600000
                    timestr = stamp2time(tweet_msecs)

                    _result = time_comparison(tweet_msecs, start_time_stamp, end_time_stamp)
                    if _result[0]:  #符合时间限制
                        if rich_output and rich_writer:
                            rec = extract_tweet_record(
                                tweet_node,
                                url_fallback_screen_name=_user_info.screen_name,
                                editable_until_msecs=editable_until,
                                context={"timeline": "conversation"},
                                include_raw_legacy=rich_include_raw_legacy,
                            )
                            if rec and rec.get("tweet_id") and rec["tweet_id"] not in rich_seen_tweet_ids:
                                rich_seen_tweet_ids.add(rec["tweet_id"])
                                rich_writer.write(rec)

                        if 'extended_entities' in legacy:
                            tweet_id = legacy.get("id_str") or tweet_node.get("rest_id")
                            tweet_url = f"https://x.com/{_user_info.screen_name}/status/{tweet_id}" if tweet_id else None
                            for _media in legacy['extended_entities']['media']:
                                if not isinstance(_media, dict):
                                    continue
                                if 'video_info' in _media:
                                    if not has_video:
                                        continue
                                    media_url = get_heighest_video_quality(_media['video_info']['variants'])
                                    prefix = f'{timestr}-vid'
                                    media_type = 'Video'
                                else:
                                    media_url = _media.get('media_url_https')
                                    prefix = f'{timestr}-img'
                                    media_type = 'Image'
                                if not media_url:
                                    continue
                                csv_info = [tweet_msecs, _user_info.name, f'@{_user_info.screen_name}', tweet_url, media_type, media_url, '', legacy.get('full_text', '')] + frr
                                media_meta = {
                                    "tweet_id": tweet_id,
                                    "tweet_url": tweet_url,
                                    "created_at_ms": tweet_msecs,
                                    "media": _media,
                                    "context": {"timeline": "conversation"},
                                }
                                _photo_lst.append((media_url, prefix, csv_info, media_meta))
                    elif not _result[1]:    #已超出目标时间范围
                        start_label = False
                        break

            except Exception as e:
                continue
            if 'cursor-bottom' in i['entryId']:     #更新下一页的请求编号(含转推模式&亮点模式)
                _user_info.cursor = i['content']['value']

        return _photo_lst

    print(f'已下载图片/视频:{_user_info.count}')
    if has_highlights: ##2024-01-05 #适配[亮点]标签
        url_top = 'https://twitter.com/i/api/graphql/w9-i9VNm_92GYFaiyGT1NA/UserHighlightsTweets?variables={"userId":"' + _user_info.rest_id + '","count":20,'
        url_bottom = '"includePromotedContent":true,"withVoice":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"c9s_tweet_anatomy_moderator_badge_enabled":true,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'
    elif has_likes:
        url_top = 'https://twitter.com/i/api/graphql/-fbTO1rKPa3nO6-XIRgEFQ/Likes?variables={"userId":"' + _user_info.rest_id + '","count":200,'
        url_bottom = '"includePromotedContent":false,"withClientEventToken":false,"withBirdwatchNotes":false,"withVoice":true,"withV2Timeline":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"c9s_tweet_anatomy_moderator_badge_enabled":true,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'
    elif has_retweet:     #包含转推调用[UserTweets]的API(调用一次上限返回20条)
        url_top = 'https://twitter.com/i/api/graphql/2GIWTr7XwadIixZDtyXd4A/UserTweets?variables={"userId":"' + _user_info.rest_id + '","count":20,'
        url_bottom = '"includePromotedContent":false,"withQuickPromoteEligibilityTweetFields":true,"withVoice":true,"withV2Timeline":true}&features={"rweb_lists_timeline_redesign_enabled":true,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}&fieldToggles={"withAuxiliaryUserLabels":false,"withArticleRichContentState":false}'
    else:       #不包含转推则调用[UserMedia]的API(返回条数貌似无上限/改count) ##2023-12-11#此模式API返回值变动
        url_top = 'https://twitter.com/i/api/graphql/Le6KlbilFmSu-5VltFND-Q/UserMedia?variables={"userId":"' + _user_info.rest_id + '","count":500,'
        url_bottom = '"includePromotedContent":false,"withClientEventToken":false,"withBirdwatchNotes":false,"withVoice":true,"withV2Timeline":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'

    if _user_info.cursor:
        url = url_top + '"cursor":"' + _user_info.cursor + '",' + url_bottom
    else:
        url = url_top + url_bottom      #第一页,无cursor
    try:
        global request_count
        resp = httpx.get(quote_url(url), headers=_headers, proxy=proxies)
        response = resp.text
        request_count += 1
        if resp.status_code == 429:
            reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
            raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
        try:
            raw_data = json.loads(response)
        except Exception:
            if 'rate limit exceeded' in str(response).lower():
                print('API次数已超限')
                reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
                raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
            else:
                print('获取数据失败')
            print(response)
            return None
        err_kind = _print_api_errors(raw_data)
        if err_kind:
            print(response)
            if err_kind == 'rate_limit':
                reset_at, retry_after = _rate_limit_reset_from_headers(resp.headers)
                raise RateLimitExceeded('Rate limit exceeded', reset_at=reset_at, retry_after=retry_after)
            return None
        if has_highlights:  #亮点模式
            raw_data = raw_data['data']['user']['result']['timeline']['timeline']['instructions'][-1]['entries']
        elif has_retweet:   #与likes共用
            raw_data = raw_data['data']['user']['result']['timeline_v2']['timeline']['instructions'][-1]['entries']
        else:   #usermedia模式
            raw_data = raw_data['data']['user']['result']['timeline_v2']['timeline']['instructions']
        if (has_retweet or has_highlights) and 'cursor-top' in raw_data[0]['entryId']:      #含转推模式 所有推文已全部下载完成
            return False
        
        if not has_retweet and not has_highlights:     #usermedia模式下的下一页请求编号
            for i in raw_data[-1]['entries']:
                if 'bottom' in i['entryId']:
                    _user_info.cursor = i['content']['value']
            # _user_info.cursor = raw_data[-1]['entries'][0]['content']['value']
        
        if start_label:     #判断是否超出时间范围
            if not has_retweet and not has_highlights:
                global First_Page
                if First_Page:   #第一页的返回值需特殊处理
                    raw_data = raw_data[-1]['entries'][0]['content']['items']
                    First_Page = False
                else:
                    if 'moduleItems' not in raw_data[0]:    #usermedia新模式，所有推文已全部下载完成
                        return False
                    else:
                        raw_data = raw_data[0]['moduleItems']
            photo_lst = get_url_from_content(raw_data)
        else:
            return False
        
        if not photo_lst:
            photo_lst.append(True)
    except RateLimitExceeded:
        raise
    except Exception as e:
        print('获取推文信息错误')
        print(e)
        print(response)
        return None
    return photo_lst

def download_control(_user_info):
    async def _main():
        # Metadata-only mode: keep calling the timeline API to emit rich_output/jsonl,
        # but skip downloading any media bytes to disk.
        if not download_media:
            while True:
                photo_lst = get_download_url(_user_info)
                if photo_lst is False:
                    return 'completed'
                if photo_lst is None:
                    return 'error'
                if photo_lst and photo_lst[0] is True:
                    continue
                if _user_info.save_path:
                    save_state(_user_info.save_path, run_key=RUN_KEY, cursor=_user_info.cursor, extra={"mode": "metadata_only"})

        async def down_save(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url, prefix, csv_info, order: int, media_meta=None):
            if '.mp4' in url:
                _file_name = f'{_user_info.save_path + os.sep}{prefix}_{_user_info.count + order}.mp4'
            else:
                try:
                    if orig_format:
                        url += f'?name=orig'
                        _file_name = f'{_user_info.save_path + os.sep}{prefix}_{_user_info.count + order}.{csv_info[5][-3:]}' # 根据图片 url 获取原始格式
                    else: # 指定格式时，先使用 name=orig，404 则切回 name=4096x4096，以保证最大尺寸
                        _file_name = f'{_user_info.save_path + os.sep}{prefix}_{_user_info.count + order}.{img_format}'
                        if img_format != 'png':
                            url += f'?format=jpg&name=4096x4096'
                        else:
                            url += f'?format=png&name=4096x4096'
                except Exception as e:
                    print(url)
                    return False

            csv_info[-5] = os.path.split(_file_name)[1]
            if md_output: # 在下载完毕之前先输出到 Markdown，以尽可能保证高并发下载也能得到正确的推文顺序。
                md_file.media_tweet_input(csv_info, prefix)
            count = 0
            while True:
                try:
                    async with semaphore:
                        global down_count
                        response = await client.get(quote_url(url))
                        if response.status_code == 404:
                            raise Exception('404')
                        if response.status_code >= 400:
                            raise httpx.HTTPStatusError(
                                f'HTTP {response.status_code}',
                                request=response.request,
                                response=response,
                            )
                        down_count += 1
                    with open(_file_name,'wb') as f:
                        f.write(response.content)

                    csv_file.data_input(csv_info)
                    if rich_output and rich_writer:
                        created_iso = datetime.fromtimestamp(int(csv_info[0]) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                        media_raw = (media_meta or {}).get("media") or {}
                        ev = {
                            "kind": "tweet_media",
                            "tweet_id": (media_meta or {}).get("tweet_id"),
                            "tweet_url": (media_meta or {}).get("tweet_url") or csv_info[3],
                            "created_at_ms": csv_info[0],
                            "created_at_iso": created_iso,
                            "author_display_name": csv_info[1],
                            "author_user_name": csv_info[2],
                            "text": csv_info[7],
                            "counts": {
                                "favorite_count": csv_info[8],
                                "retweet_count": csv_info[9],
                                "reply_count": csv_info[10],
                            },
                            "media_type": csv_info[4],
                            "media_url": csv_info[5],
                            "media_id_str": media_raw.get("id_str") if isinstance(media_raw, dict) else None,
                            "media_expanded_url": media_raw.get("expanded_url") if isinstance(media_raw, dict) else None,
                            "media_display_url": media_raw.get("display_url") if isinstance(media_raw, dict) else None,
                            "local_file": os.path.split(_file_name)[1],
                            "local_path": _file_name,
                        }
                        ctx = (media_meta or {}).get("context")
                        if ctx:
                            ev["context"] = ctx
                        rich_writer.write(ev)

                    if log_output:
                        print(f'{_file_name}=====>下载完成')

                    break
                except Exception as e:
                    if not ('.mp4' in url or orig_format or str(e) != "404"):
                        url = url.replace('name=orig', 'name=4096x4096')
                        continue
                    count += 1
                    if count >= 50:
                        print(f'{_file_name}=====>第{count}次下载失败，已跳过该文件。')
                        print(url)
                        print(f'原因: {type(e).__name__}: {e}')
                        break
                    # 降低刷屏：默认仅打印异常类型；如需更多细节可打开 settings.json 的 log_output
                    if log_output:
                        print(f'{_file_name}=====>第{count}次下载失败: {type(e).__name__}: {e}')
                    else:
                        print(f'{_file_name}=====>第{count}次下载失败,正在重试')
                    print(url)

        try:
            timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
            limits = httpx.Limits(max_connections=max_concurrent_requests, max_keepalive_connections=max_concurrent_requests)
            download_headers = {'user-agent': _headers.get('user-agent', 'Mozilla/5.0')}
            async with httpx.AsyncClient(
                proxy=proxies,
                timeout=timeout,
                limits=limits,
                headers=download_headers,
                follow_redirects=True,
            ) as client:
                while True:
                    photo_lst = get_download_url(_user_info)
                    if photo_lst is False:
                        return 'completed'
                    if photo_lst is None:
                        return 'error'
                    elif photo_lst[0] == True:
                        continue
                    semaphore = asyncio.Semaphore(max_concurrent_requests)    #最大并发数量，默认为8，对自己网络有自信的可以调高
                    if down_log:
                        await asyncio.gather(*[asyncio.create_task(down_save(client, semaphore, url[0], url[1], url[2], order, url[3] if len(url) > 3 else None)) for order,url in enumerate(photo_lst) if cache_data.is_present(url[0])])
                    else:
                        await asyncio.gather(*[asyncio.create_task(down_save(client, semaphore, url[0], url[1], url[2], order, url[3] if len(url) > 3 else None)) for order,url in enumerate(photo_lst)])
                    _user_info.count += len(photo_lst)      #更新计数
                    if _user_info.save_path:
                        save_state(_user_info.save_path, run_key=RUN_KEY, cursor=_user_info.cursor, extra={"downloaded_count": _user_info.count})
        except RateLimitExceeded as e:
            if _user_info.save_path:
                save_state(
                    _user_info.save_path,
                    run_key=RUN_KEY,
                    cursor=_user_info.cursor,
                    extra={
                        "downloaded_count": infer_existing_media_count(_user_info.save_path),
                        "last_error": str(e),
                        "rate_limit_reset_at": e.reset_at,
                        "rate_limit_retry_after": e.retry_after,
                    },
                )
            if e.reset_at:
                try:
                    reset_local = datetime.fromtimestamp(int(e.reset_at))
                    print(f'API次数已超限，预计可用时间: {reset_local}')
                except Exception:
                    print('API次数已超限')
            else:
                print('API次数已超限')
            return 'rate_limited'
        except Exception as e:
            if _user_info.save_path:
                save_state(
                    _user_info.save_path,
                    run_key=RUN_KEY,
                    cursor=_user_info.cursor,
                    extra={"downloaded_count": infer_existing_media_count(_user_info.save_path), "last_error": str(e)},
                )
            raise

    return asyncio.run(_main())

def main(_user_info: object):
    if not _ensure_csrf_headers(_headers):
        return False
    _headers['referer'] = 'https://twitter.com/' + _user_info.screen_name
    try:
        if not get_other_info(_user_info):
            return False
    except RateLimitExceeded as e:
        print('API次数已超限，已中断。')
        if e.reset_at:
            try:
                reset_local = datetime.fromtimestamp(int(e.reset_at))
                print(f'可用时间(估计): {reset_local}')
            except Exception:
                pass
        return 'rate_limited'
    print_info(_user_info)
    _path = settings['save_path'] + _user_info.screen_name
    if not os.path.exists(_path):   #创建文件夹
        os.makedirs(settings['save_path']+_user_info.screen_name)       #用户名建文件夹
        _user_info.save_path = settings['save_path']+_user_info.screen_name
    else:
        _user_info.save_path = _path

    # 避免重复运行覆盖同名文件；同时为恢复下载提供正确的计数起点
    _user_info.count = infer_existing_media_count(_user_info.save_path)

    # 自动恢复上次未完成的 cursor（仅在同一配置模式下）
    state = load_state(_user_info.save_path, run_key=RUN_KEY)
    if state and state.get('cursor'):
        _user_info.cursor = state.get('cursor')
        global First_Page
        First_Page = False
        print(f'检测到未完成进度，已从 cursor 继续: {str(_user_info.cursor)[:24]}...')

    global csv_file
    csv_file = None
    if download_media:
        csv_file = csv_gen(_user_info.save_path, _user_info.name, _user_info.screen_name, settings['time_range'])

    if md_output and download_media:
        global md_file
        md_file = md_gen(_user_info.save_path, _user_info.name, _user_info.screen_name, settings['time_range'], has_likes, media_count_limit)

    if down_log and download_media:
        global cache_data
        cache_data = cache_gen(_user_info.save_path)

    global rich_writer, rich_seen_tweet_ids
    if rich_output:
        rich_seen_tweet_ids = set()
        rich_path = Path(_user_info.save_path) / f'{_user_info.screen_name}-{datetime.now().strftime("%Y-%m-%d_%H-%M-%S")}-rich.jsonl'
        rich_writer = JsonlWriter(rich_path)

    if autoSync:
        files = sorted(os.listdir(_user_info.save_path))
        if len(files) > 0:
            global start_time_stamp
            re_rule = r'\d{4}-\d{2}-\d{2}'
            for i in files[::-1]:
                if "-img_" in i:
                    start_time_stamp = time2stamp(re.findall(re_rule, i)[0])
                    break
                elif "-vid_" in i:
                    start_time_stamp = time2stamp(re.findall(re_rule, i)[0])
                    break
                else:
                    start_time_stamp = backup_stamp
        else:
            start_time_stamp = backup_stamp

    status = download_control(_user_info)

    if csv_file is not None:
        csv_file.csv_close()
    
    if md_output and md_file is not None:
        md_file.md_close()

    if rich_output and rich_writer:
        rich_writer.close()
        rich_writer = None

    if down_log and cache_data is not None:
        del cache_data
    if status == 'completed':
        clear_state(_user_info.save_path)
        print(f'{_user_info.name}下载完成\n\n')
        return True
    elif status == 'rate_limited':
        print(f'{_user_info.name} 下载中断：API次数已超限，已保存进度到 {_user_info.save_path}/.crawl_state.json\n')
        return 'rate_limited'
    elif status == 'error':
        print(f'{_user_info.name} 下载中断：请求/解析失败（已保存进度到 {_user_info.save_path}/.crawl_state.json）\n')
        return False
    else:
        print(f'{_user_info.name} 下载中断：未知原因（已保存进度到 {_user_info.save_path}/.crawl_state.json）\n')
        return False

if __name__=='__main__':
    _start = time.time()
    if '--search' in sys.argv:
        idx = sys.argv.index('--search')
        forward_argv = sys.argv[idx + 1 :]
        import search_down
        search_down.main(forward_argv)
        sys.exit(0)

    user_list_raw = settings.get('user_lst', '')

    def _parse_users(raw_list):
        users = []
        for item in raw_list:
            for part in str(item).split(','):
                part = part.strip()
                if part:
                    users.append(part.lstrip('@'))
        return users

    cli_users = _parse_users(sys.argv[1:])
    if cli_users:
        user_list = cli_users
    elif str(user_list_raw).strip():
        user_list = _parse_users([user_list_raw])
    else:
        user_list = []
    if not user_list:
        search_query = str(settings.get('search_query', '')).strip()
        if search_query:
            import search_down
            search_down.main([])
            sys.exit(0)
        print('未配置 user_lst，也未从命令行获取到用户名。')
        print('方式1: 在 settings.json 中填写 user_lst，例如: "user1,user2"')
        print('方式2: 命令行传入用户名，例如: python3 main.py user1,user2  或  python3 main.py user1 user2')
        print('方式3: 关键词搜索(不限制用户)：python3 main.py --search \"关键词 filter:media\" --count 200')
        sys.exit(1)

    for i in user_list:
        result = main(User_info(i))
        start_label = True
        First_Page = True
        if result == 'rate_limited':
            break
    print(f'共耗时:{time.time()-_start}秒\n共调用{request_count}次API\n共下载{down_count}份图片/视频')
