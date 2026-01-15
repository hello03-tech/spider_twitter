# 推特爬虫

主要参考https://github.com/caolvchong-top/twitter_download
并根据特定需求做出一定的导出修改

推特 图片 & 视频 & 文本 下载，以用户名为参数，爬取该用户推文中的图片与视频(含gif)

支持排除转推内容 & 多用户爬取 & 时间范围限制 & 按Tag获取 & 纯文本获取 & 高级搜索 & 评论区下载


部署
--- 

**Linux** : 
``` 
cd twitter_download 
pip3 install -r requirements.txt

#Python版本须>=3.8  httpx==0.28.1
``` 
**运行** : 
``` 
配置settings.json文件 写入cookie
./run_spider.sh

# (可选) 不修改 settings.json 的 user_lst，直接从命令行传入用户名
# 例如:
python3 main.py user1,user2
# 或
python3 main.py user1 user2

# (可选) 关键词搜索下载(不限制用户)
python3 search_down.py "openai lang:zh filter:media -filter:replies" --count 200
# 默认输出为 JSONL 记录文件（仍会下载媒体文件）；如需 CSV：加 --format csv
python3 search_down.py "openai lang:zh filter:media -filter:replies" --count 200 --format csv
# 或通过 main.py 转发:
python3 main.py --search "openai lang:zh filter:media -filter:replies" --count 200
``` 
**Windows** 和上面的一样，配置完setting.json后运行main.py即可 

新增输出（可选）
---
在 `settings.json` 中开启 `rich_output` 后，`main.py` 会在每个用户目录下额外输出 `*-rich.jsonl`，包含尽可能多的推文/媒体元信息（时间、推文URL、文本、实体信息、媒体信息、本地文件路径等）。

`reply_down.py` 也会在目标目录下额外输出 `*-Reply.jsonl`（可在脚本顶部开关 `rich_output`）。

如果你只需要导出「日期 / URL / 文本」的汇总内容，`export_content.py` 也支持输出为 JSON/JSONL：
```bash
python3 export_content.py --format json  -o exported_content.json
python3 export_content.py --format jsonl -o exported_content.jsonl
```


注意事项
---

**按Tag下载&高级搜索 --> tag_down.py** 

**下载评论区 --> reply_down.py** 

**指定用户纯文本推文获取 --> text_down.py** 

**指定用户媒体文件获取&转推&亮点&喜欢(只能本人账号)等 --> main.py + settings.json** 

其余各种不能解决的需求建议试试tag_down的高级搜索, 或是提交Issue 


Tag_Down 功能扩展 (高级搜索) &nbsp;&nbsp; <sub>//万金油</sub> 
---
~~其实按功能应该叫`search_down`~~

对于部分主程序难以实现的需求可以尝试配置`tag_down.py`的`filter`来曲线解决: 

|部分例子|
|:--:|
|大批量下载 -> 分批下载|
|指定时间范围|
|各类关键词搜索/排除|
|指定/排除目标用户|
|指定大于互动量的推文|
|指定推文语言|
|......| 

``` 
``` 
推特高级搜索：https://x.com/search-advanced 

实例参考：https://github.com/caolvchong-top/twitter_download/issues/63#issuecomment-2351039320 & https://github.com/caolvchong-top/twitter_download/issues/106


 
