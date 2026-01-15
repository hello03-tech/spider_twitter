import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple
from urllib.parse import quote

import httpx

from transaction_generate import get_transaction_id, get_url_path
from url_utils import quote_url, cookie_get, require_cookie_fields


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


def del_special_char(string: str) -> str:
    # folder-safe: keep CJK, alnum, some JP ranges, and a few separators
    return re.sub(r'[^\u4e00-\u9fa5\u0030-\u0039\u0041-\u005a\u0061-\u007a\u3040-\u31FF#\.\-_ ]', '', string).strip()


def stamp2time(msecs_stamp: int) -> str:
    time_array = time.localtime(msecs_stamp / 1000)
    return time.strftime("%Y-%m-%d %H-%M", time_array)


def hash_save_token(media_url: str) -> str:
    m = hashlib.md5()
    m.update(media_url.encode('utf-8'))
    return m.hexdigest()[:4]


def get_highest_video_quality(variants) -> str:
    if len(variants) == 1:
        return variants[0]['url']
    max_bitrate = -1
    best = None
    for v in variants:
        if 'bitrate' in v and int(v['bitrate']) > max_bitrate:
            max_bitrate = int(v['bitrate'])
            best = v['url']
    return best


def unwrap_tweet_result(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    if 'tweet' in result and isinstance(result['tweet'], dict):
        return result['tweet']
    return result


class CsvGen:
    def __init__(self, save_path: str, mode: str):
        os.makedirs(save_path, exist_ok=True)
        self.rows_written = 0
        self.f = open(
            f'{save_path}/{datetime.now().strftime("%Y-%m-%d %H-%M-%S")}-{mode}.csv',
            'w',
            encoding='utf-8-sig',
            newline='',
        )
        self.writer = csv.writer(self.f)
        self.writer.writerow(['Run Time : ' + datetime.now().strftime('%Y-%m-%d %H-%M-%S')])
        if mode == 'text':
            self.writer.writerow(
                [
                    'Tweet Date',
                    'Display Name',
                    'User Name',
                    'Tweet URL',
                    'Tweet Content',
                    'Favorite Count',
                    'Retweet Count',
                    'Reply Count',
                ]
            )
        else:
            self.writer.writerow(
                [
                    'Tweet Date',
                    'Display Name',
                    'User Name',
                    'Tweet URL',
                    'Media Type',
                    'Media URL',
                    'Saved Path',
                    'Tweet Content',
                    'Favorite Count',
                    'Retweet Count',
                    'Reply Count',
                ]
            )

    def close(self):
        self.f.close()

    def write_row(self, row: list):
        row[0] = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(row[0]) / 1000))
        self.writer.writerow(row)
        self.rows_written += 1


def _try_parse_msecs(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except Exception:
        return None


def _format_msecs(ms: Optional[int]) -> Optional[str]:
    if not isinstance(ms, int):
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


class JsonlGen:
    def __init__(self, save_path: str, mode: str):
        os.makedirs(save_path, exist_ok=True)
        self.rows_written = 0
        self.mode = mode
        self.f = open(
            f'{save_path}/{datetime.now().strftime("%Y-%m-%d %H-%M-%S")}-{mode}.jsonl',
            'w',
            encoding='utf-8',
            newline='\n',
        )

    def close(self):
        self.f.close()

    def write_row(self, row: list):
        record = self._row_to_record(row)
        if record is None:
            return
        self.f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.rows_written += 1

    def _row_to_record(self, row: list) -> Optional[Dict[str, Any]]:
        ms = _try_parse_msecs(row[0] if row else None)
        base: Dict[str, Any] = {"tweet_date_ms": ms, "tweet_date": _format_msecs(ms)}

        if self.mode == 'text':
            if len(row) < 8:
                return None
            base.update(
                {
                    "display_name": row[1],
                    "user_name": row[2],
                    "tweet_url": row[3],
                    "tweet_content": row[4],
                    "favorite_count": row[5],
                    "retweet_count": row[6],
                    "reply_count": row[7],
                }
            )
            return base

        if len(row) < 11:
            return None
        base.update(
            {
                "display_name": row[1],
                "user_name": row[2],
                "tweet_url": row[3],
                "media_type": row[4],
                "media_url": row[5],
                "saved_path": row[6],
                "tweet_content": row[7],
                "favorite_count": row[8],
                "retweet_count": row[9],
                "reply_count": row[10],
            }
        )
        return base


class JsonGen:
    def __init__(self, save_path: str, mode: str, *, pretty: bool = False):
        os.makedirs(save_path, exist_ok=True)
        self.rows_written = 0
        self.mode = mode
        self.pretty = pretty
        self._first = True
        self.f = open(
            f'{save_path}/{datetime.now().strftime("%Y-%m-%d %H-%M-%S")}-{mode}.json',
            'w',
            encoding='utf-8',
            newline='\n',
        )
        self.f.write("[\n" if pretty else "[")

    def close(self):
        self.f.write("\n]\n" if self.pretty else "]\n")
        self.f.close()

    def write_row(self, row: list):
        record = self._row_to_record(row)
        if record is None:
            return

        if not self._first:
            self.f.write(",\n" if self.pretty else ",")
        self._first = False

        if self.pretty:
            self.f.write(json.dumps(record, ensure_ascii=False, indent=2))
        else:
            self.f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        self.rows_written += 1

    def _row_to_record(self, row: list) -> Optional[Dict[str, Any]]:
        ms = _try_parse_msecs(row[0] if row else None)
        base: Dict[str, Any] = {"tweet_date_ms": ms, "tweet_date": _format_msecs(ms)}

        if self.mode == 'text':
            if len(row) < 8:
                return None
            base.update(
                {
                    "display_name": row[1],
                    "user_name": row[2],
                    "tweet_url": row[3],
                    "tweet_content": row[4],
                    "favorite_count": row[5],
                    "retweet_count": row[6],
                    "reply_count": row[7],
                }
            )
            return base

        if len(row) < 11:
            return None
        base.update(
            {
                "display_name": row[1],
                "user_name": row[2],
                "tweet_url": row[3],
                "media_type": row[4],
                "media_url": row[5],
                "saved_path": row[6],
                "tweet_content": row[7],
                "favorite_count": row[8],
                "retweet_count": row[9],
                "reply_count": row[10],
            }
        )
        return base


async def download_control(
    media_lst: List[Tuple[str, List[Any], bool]],
    csv_writer,
    max_concurrent_requests: int,
    proxy: Optional[str],
    *,
    verbose: bool = True,
):
    semaphore = asyncio.Semaphore(max_concurrent_requests)
    total = len(media_lst)
    completed = 0
    last_print = 0.0
    print_lock = asyncio.Lock()

    def _maybe_print_progress(done: int, *, final: bool = False) -> None:
        nonlocal last_print
        if not verbose:
            return
        now = time.monotonic()
        if not final and now - last_print < 0.25:
            return
        last_print = now
        msg = f'下载进度: {done}/{total}'
        print(msg, end='\r' if not final else '\n', flush=True)

    async def down_save(url: str, csv_info: list, is_image: bool):
        nonlocal completed
        if is_image:
            url += '?format=png&name=4096x4096'

        count = 0
        while True:
            try:
                async with semaphore:
                    async with httpx.AsyncClient(proxy=proxy) as client:
                        response = await client.get(quote_url(url), timeout=(3.05, 16))
                with open(csv_info[6], 'wb') as f:
                    f.write(response.content)
                break
            except Exception as e:
                count += 1
                print(e)
                print(f'{csv_info[6]}=====>第{count}次下载失败,正在重试')
        csv_writer.write_row(csv_info)
        async with print_lock:
            completed += 1
            _maybe_print_progress(completed, final=(completed == total))

    if verbose and total:
        _maybe_print_progress(0)
    await asyncio.gather(*[asyncio.create_task(down_save(url, csv_info, is_image)) for url, csv_info, is_image in media_lst])


class SearchDown:
    def __init__(
        self,
        cookie: str,
        raw_query: str,
        save_path: str,
        down_count: int,
        media_latest: bool,
        text_down: bool,
        max_concurrent_requests: int,
        proxy: Optional[str],
        folder_name: Optional[str],
        output_format: str,
        json_pretty: bool,
        no_media: bool = False,
        *,
        verbose: bool = True,
    ):
        self.cookie = cookie
        self.raw_query = raw_query
        self.down_count = down_count
        self.media_latest = media_latest
        self.text_down = text_down
        self.max_concurrent_requests = max_concurrent_requests
        self.proxy = proxy
        self.verbose = verbose
        self.no_media = bool(no_media)

        if text_down:
            self.entries_count = 20
            self.product = 'Latest'
            self.mode = 'text'
        else:
            self.entries_count = 50
            self.product = 'Media'
            self.mode = 'media_latest' if media_latest else 'media'
            if media_latest:
                self.entries_count = 20
                self.product = 'Latest'

        folder = folder_name if folder_name else del_special_char(raw_query)[:120]
        folder = folder or 'search'
        self.folder_path = os.path.join(save_path, folder) + os.sep
        os.makedirs(self.folder_path, exist_ok=True)

        mode_label = self.mode if not text_down else 'text'
        if output_format == 'csv':
            self.csv = CsvGen(self.folder_path, mode_label)
        elif output_format == 'jsonl':
            self.csv = JsonlGen(self.folder_path, mode_label)
        elif output_format == 'json':
            self.csv = JsonGen(self.folder_path, mode_label, pretty=json_pretty)
        else:
            raise ValueError(f'Unsupported output_format: {output_format}')
        self.cursor = ''

        self._headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
            'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
            'cookie': cookie,
        }
        require_cookie_fields(cookie, 'auth_token', 'ct0')
        self._headers['x-csrf-token'] = cookie_get(cookie, 'ct0')
        self._headers['referer'] = f'https://twitter.com/search?q={quote(raw_query)}&src=typed_query&f=media'

        self.ct = get_transaction_id()

    def _build_url(self) -> str:
        url = (
            'https://x.com/i/api/graphql/AIdc203rPpK_k_2KWSdm7g/SearchTimeline'
            + '?variables={"rawQuery":"'
            + quote(self.raw_query)
            + '","count":'
            + str(self.entries_count)
            + ',"cursor":"'
            + self.cursor
            + '","querySource":"typed_query","product":"'
            + self.product
            + '"}'
            + '&features={"rweb_video_screen_enabled":false,"profile_label_improvements_pcf_label_in_post_enabled":true,"rweb_tipjar_consumption_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"premium_content_api_read_enabled":false,"communities_web_enable_tweet_community_results_fetch":true,"c9s_tweet_anatomy_moderator_badge_enabled":true,"responsive_web_grok_analyze_button_fetch_trends_enabled":false,"responsive_web_grok_analyze_post_followups_enabled":true,"responsive_web_jetfuel_frame":false,"responsive_web_grok_share_attachment_enabled":true,"articles_preview_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":true,"tweet_awards_web_tipping_enabled":false,"responsive_web_grok_show_grok_translated_post":false,"responsive_web_grok_analysis_button_from_backend":false,"creator_subscriptions_quote_tweet_preview_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_grok_image_annotation_enabled":true,"responsive_web_enhance_cards_enabled":false}'
        )
        _path = get_url_path(url)
        url = quote_url(url)
        self._headers['x-client-transaction-id'] = self.ct.generate_transaction_id(method='GET', path=_path)
        return url

    def _get_json(self, url: str) -> Optional[Dict[str, Any]]:
        try:
            response = httpx.get(url, headers=self._headers, proxy=self.proxy, timeout=(3.05, 16)).text
        except Exception as e:
            print(f'请求失败: {e}')
            return None
        try:
            data = json.loads(response)
            if isinstance(data, dict) and data.get('errors'):
                first = data['errors'][0] if isinstance(data['errors'], list) and data['errors'] else data['errors']
                code = first.get('code') if isinstance(first, dict) else None
                msg = first.get('message') if isinstance(first, dict) else str(first)
                print(f'API错误: {code} {msg}')
                if code == 353 or 'csrf' in str(msg).lower():
                    print('提示: 需要 cookie 中的 ct0 与请求头 x-csrf-token 匹配；请更新/检查 cookie。')
                return None
            return data
        except Exception:
            if 'Rate limit exceeded' in response:
                print('API次数已超限')
            else:
                print('获取数据失败')
            print(response)
            return None

    def _extract_first_entries(self, raw_data: dict):
        entries = raw_data['data']['search_by_raw_query']['search_timeline']['timeline']['instructions'][-1]['entries']
        if len(entries) == 2:
            return None
        self.cursor = entries[-1]['content']['value']
        return entries

    def _extract_next_entries(self, raw_data: dict):
        instructions = raw_data['data']['search_by_raw_query']['search_timeline']['timeline']['instructions']
        self.cursor = instructions[-1]['entry']['content']['value']
        return instructions

    def search_media(self, url: str):
        media_lst = []
        raw_data = self._get_json(url)
        if not raw_data:
            return None

        if not self.cursor:
            entries = self._extract_first_entries(raw_data)
            if not entries:
                return None
            raw_data_lst = entries[0]['content']['items']
        else:
            instructions = self._extract_next_entries(raw_data)
            if len(instructions) == 2:
                return None
            first = instructions[0]
            if 'moduleItems' in first:
                raw_data_lst = first['moduleItems']
            else:
                return None

        for tweet in raw_data_lst:
            tweet = unwrap_tweet_result(tweet['item']['itemContent']['tweet_results']['result'])
            if not tweet:
                continue

            try:
                display_name = tweet['core']['user_results']['result']['legacy']['name']
                screen_name = '@' + tweet['core']['user_results']['result']['legacy']['screen_name']
            except Exception:
                continue
            try:
                time_stamp = int(tweet['edit_control']['editable_until_msecs']) - 3600000
            except Exception:
                if 'edit_control_initial' in tweet.get('edit_control', {}):
                    time_stamp = int(tweet['edit_control']['edit_control_initial']['editable_until_msecs']) - 3600000
                else:
                    continue
            try:
                favorite_count = tweet['legacy']['favorite_count']
                retweet_count = tweet['legacy']['retweet_count']
                reply_count = tweet['legacy']['reply_count']
                status_id = tweet['rest_id']
                tweet_url = f'https://twitter.com/{screen_name}/status/{status_id}'
                full_text = tweet['legacy']['full_text']
                tweet_content = re.sub(r'https?://t\.co/\w+\s*$', '', full_text).strip()
            except Exception:
                continue

            try:
                raw_media_lst = tweet['legacy']['extended_entities']['media']
                for media in raw_media_lst:
                    if 'video_info' in media:
                        media_url = get_highest_video_quality(media['video_info']['variants'])
                        if not media_url:
                            continue
                        media_type = 'Video'
                        is_image = False
                        file_name = f'{self.folder_path}{stamp2time(time_stamp)}_{screen_name}_{hash_save_token(media_url)}.mp4'
                    else:
                        media_url = media['media_url_https']
                        media_type = 'Image'
                        is_image = True
                        file_name = f'{self.folder_path}{stamp2time(time_stamp)}_{screen_name}_{hash_save_token(media_url)}.png'
                    csv_info = [
                        time_stamp,
                        display_name,
                        screen_name,
                        tweet_url,
                        media_type,
                        media_url,
                        file_name,
                        tweet_content,
                        favorite_count,
                        retweet_count,
                        reply_count,
                    ]
                    media_lst.append([media_url, csv_info, is_image])
            except KeyError:
                pass
            except Exception as e:
                print(e)

        return media_lst

    def search_media_latest(self, url: str):
        media_lst = []
        raw_data = self._get_json(url)
        if not raw_data:
            return None

        if not self.cursor:
            entries = self._extract_first_entries(raw_data)
            if not entries:
                return None
            raw_data_lst = entries[:-2]
        else:
            instructions = self._extract_next_entries(raw_data)
            if len(instructions) == 2:
                return None
            first = instructions[0]
            if 'entries' in first:
                raw_data_lst = first['entries']
            else:
                return None

        for tweet in raw_data_lst:
            if 'promoted' in tweet.get('entryId', ''):
                continue
            tweet = unwrap_tweet_result(tweet['content']['itemContent']['tweet_results']['result'])
            if not tweet:
                continue
            try:
                display_name = tweet['core']['user_results']['result']['legacy']['name']
                screen_name = '@' + tweet['core']['user_results']['result']['legacy']['screen_name']
            except Exception:
                continue
            try:
                time_stamp = int(tweet['edit_control']['editable_until_msecs']) - 3600000
            except Exception:
                if 'edit_control_initial' in tweet.get('edit_control', {}):
                    time_stamp = int(tweet['edit_control']['edit_control_initial']['editable_until_msecs']) - 3600000
                else:
                    continue
            try:
                favorite_count = tweet['legacy']['favorite_count']
                retweet_count = tweet['legacy']['retweet_count']
                reply_count = tweet['legacy']['reply_count']
                status_id = tweet['rest_id']
                tweet_url = f'https://twitter.com/{screen_name}/status/{status_id}'
                full_text = tweet['legacy']['full_text']
                tweet_content = re.sub(r'https?://t\.co/\w+\s*$', '', full_text).strip()
            except Exception:
                continue

            try:
                raw_media_lst = tweet['legacy']['extended_entities']['media']
                for media in raw_media_lst:
                    if 'video_info' in media:
                        media_url = get_highest_video_quality(media['video_info']['variants'])
                        if not media_url:
                            continue
                        media_type = 'Video'
                        is_image = False
                        file_name = f'{self.folder_path}{stamp2time(time_stamp)}_{screen_name}_{hash_save_token(media_url)}.mp4'
                    else:
                        media_url = media['media_url_https']
                        media_type = 'Image'
                        is_image = True
                        file_name = f'{self.folder_path}{stamp2time(time_stamp)}_{screen_name}_{hash_save_token(media_url)}.png'
                    csv_info = [
                        time_stamp,
                        display_name,
                        screen_name,
                        tweet_url,
                        media_type,
                        media_url,
                        file_name,
                        tweet_content,
                        favorite_count,
                        retweet_count,
                        reply_count,
                    ]
                    media_lst.append([media_url, csv_info, is_image])
            except KeyError:
                pass
            except Exception as e:
                print(e)

        return media_lst

    def search_save_text(self, url: str) -> bool:
        raw_data = self._get_json(url)
        if not raw_data:
            return False

        if not self.cursor:
            entries = self._extract_first_entries(raw_data)
            if not entries:
                return False
            raw_data_lst = entries[:-2]
        else:
            instructions = self._extract_next_entries(raw_data)
            if len(instructions) == 2:
                return False
            first = instructions[0]
            raw_data_lst = first.get('entries', [])

        for tweet in raw_data_lst:
            if 'promoted' in tweet.get('entryId', ''):
                continue
            tweet = unwrap_tweet_result(tweet['content']['itemContent']['tweet_results']['result'])
            if not tweet:
                continue
            if 'tweet' in tweet and 'edit_control' in tweet['tweet']:
                tweet = tweet['tweet']

            try:
                time_stamp = int(tweet['edit_control']['editable_until_msecs']) - 3600000
            except Exception:
                if 'edit_control_initial' in tweet.get('edit_control', {}):
                    time_stamp = int(tweet['edit_control']['edit_control_initial']['editable_until_msecs']) - 3600000
                else:
                    continue
            try:
                display_name = tweet['core']['user_results']['result']['legacy']['name']
                screen_name = '@' + tweet['core']['user_results']['result']['legacy']['screen_name']
            except Exception:
                continue

            try:
                favorite_count = tweet['legacy']['favorite_count']
                retweet_count = tweet['legacy']['retweet_count']
                reply_count = tweet['legacy']['reply_count']
                status_id = tweet['rest_id']
                tweet_url = f'https://twitter.com/{screen_name}/status/{status_id}'
                full_text = tweet['legacy']['full_text']
                tweet_content = re.sub(r'https?://t\.co/\w+\s*$', '', full_text).strip()
            except Exception:
                continue

            self.csv.write_row(
                [
                    time_stamp,
                    display_name,
                    screen_name,
                    tweet_url,
                    tweet_content,
                    favorite_count,
                    retweet_count,
                    reply_count,
                ]
            )
        return True

    def run(self):
        pages = max(1, (self.down_count + self.entries_count - 1) // self.entries_count) if self.down_count else 1
        if self.verbose:
            mode_label = 'text' if self.text_down else ('media_latest' if self.media_latest else 'media')
            print(f'开始搜索: {self.raw_query}')
            print(f'模式: {mode_label} | 每页: {self.entries_count} | 目标: {self.down_count} | 预计页数: {pages}')
            print(f'保存目录: {self.folder_path}')

        for page_idx in range(1, pages + 1):
            url = self._build_url()
            if self.verbose:
                cursor_label = (self.cursor[:60] + '...') if self.cursor and len(self.cursor) > 60 else (self.cursor or '(first)')
                print(f'\n[{page_idx}/{pages}] 拉取中... cursor={cursor_label}', flush=True)

            if self.text_down:
                before = self.csv.rows_written
                if not self.search_save_text(url):
                    break
                added = self.csv.rows_written - before
                if self.verbose:
                    print(f'本页写入 {added} 条文本', flush=True)
            else:
                if self.media_latest:
                    media_lst = self.search_media_latest(url)
                else:
                    media_lst = self.search_media(url)
                if not media_lst:
                    break
                before = self.csv.rows_written
                if self.no_media:
                    # Record-only mode: do not fetch media bytes.
                    for _, csv_info, _ in media_lst:
                        if isinstance(csv_info, list) and len(csv_info) >= 7:
                            csv_info[6] = ''
                        self.csv.write_row(csv_info)
                    if self.verbose:
                        added = self.csv.rows_written - before
                        print(f'本页写入记录 {added}/{len(media_lst)} (不下载媒体)', flush=True)
                else:
                    if self.verbose:
                        print(f'本页解析到 {len(media_lst)} 个媒体，开始下载... (并发={self.max_concurrent_requests})', flush=True)
                    asyncio.run(
                        download_control(
                            media_lst,
                            self.csv,
                            self.max_concurrent_requests,
                            self.proxy,
                            verbose=self.verbose,
                        )
                    )
                    if self.verbose:
                        added = self.csv.rows_written - before
                        print(f'本页下载完成 {added}/{len(media_lst)}', flush=True)
        self.csv.close()
        if self.verbose:
            print(f'\n完成：共写入 {self.csv.rows_written} 条记录', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Search keywords to download Twitter/X media or text (not limited to a user).')
    parser.add_argument('query', nargs='?', help='Search keyword/filter, e.g. "openai lang:zh filter:media -filter:replies"')
    parser.add_argument('--count', type=int, default=None, help='Approx total results to process (default: settings.search_down_count or 100)')
    parser.add_argument('--latest', action='store_true', help='Use [Latest] tab (default is [Media])')
    parser.add_argument('--text', action='store_true', help='Text-only mode (consumes lots of API calls)')
    parser.add_argument('--folder', default=None, help='Output folder name (default: sanitized query)')
    parser.add_argument('--format', choices=['csv', 'json', 'jsonl'], default='jsonl', help='Record output file format (default: jsonl)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON (only when --format json)')
    parser.add_argument('--no-media', action='store_true', help='Do not download media files; only write JSON/CSV records')
    parser.add_argument('--workers', type=int, default=None, help='Override max concurrent requests (default: settings.max_concurrent_requests or 8)')
    parser.add_argument('--quiet', action='store_true', help='Disable progress output')
    parser.add_argument('--settings', default='settings.json', help='Path to settings.json')

    args = parser.parse_args(argv)
    settings = load_settings(args.settings)

    query = args.query or settings.get('search_query', '')
    query = str(query).strip()
    if not query:
        print('请提供搜索关键词：')
        print('  方式1: 命令行，例如: python3 search_down.py \"openai lang:zh filter:media\"')
        print('  方式2: settings.json 填写 search_query')
        raise SystemExit(1)

    cookie = str(settings.get('cookie', '')).strip()
    if not cookie or 'auth_token=' not in cookie or 'ct0=' not in cookie:
        print('settings.json 的 cookie 需要至少包含 auth_token 与 ct0')
        raise SystemExit(1)

    save_path = settings.get('save_path') or os.path.join(os.getcwd(), 'data')
    proxy = settings.get('proxy') or None
    max_concurrent_requests = int(settings.get('max_concurrent_requests') or 8)
    if args.workers is not None:
        max_concurrent_requests = int(args.workers)
    else:
        env_workers = os.getenv('WORKERS') or os.getenv('MAX_CONCURRENT_REQUESTS')
        if env_workers:
            try:
                max_concurrent_requests = int(env_workers)
            except Exception:
                pass
    down_count = args.count if args.count is not None else int(settings.get('search_down_count') or 100)
    verbose = not bool(args.quiet or settings.get('search_quiet'))

    SearchDown(
        cookie=cookie,
        raw_query=query,
        save_path=save_path,
        down_count=down_count,
        media_latest=args.latest,
        text_down=args.text,
        max_concurrent_requests=max_concurrent_requests,
        proxy=proxy,
        folder_name=args.folder,
        output_format=args.format,
        json_pretty=bool(args.pretty),
        no_media=bool(args.no_media),
        verbose=verbose,
    ).run()


if __name__ == '__main__':
    main()
