# drpyS Node sidecar 运行时

让 Node.js（v24）直接跑 drpyS（QuickJS 风格）的 `.js` 站点脚本，以**常驻 sidecar**
（stdin/stdout JSONL）暴露统一 catvod 协议。纯 JS、零网络依赖（只使用 Node 内置模块
与本目录已 vendor 的文件）。

## 启动

```bash
cd /vol1/1000/Ai工作区/fnmedia-ext/proxy/assets/drpy
/var/apps/nodejs_v24/target/bin/node --import ./register.mjs sidecar.mjs
```

- `register.mjs` 注册同目录 `loader.mjs` 的 ESM customization hooks（Node 20+/24 稳定方式，
  无需 `--experimental-loader` 的弃用警告）。
- 进程常驻：从 stdin 读 JSONL 请求，向 stdout 写 JSONL 响应，日志全部走 stderr。
- 自检：`./build.sh`（`node --check` + 实际跑通 txbei 的 home/category）。

## JSONL 协议

stdin 每行一个请求（UTF-8，参数走 JSON 不走 argv，避免中文编码问题）：

```json
{"id":"1","engine":"drpy",
 "site":{"api":"./drpy2.min.js","ext":"./txbei.js"},
 "method":"home","args":[]}
```

- `site.api`：引擎 `drpy2.min.js` 的本地路径 / file URL / http URL（当前进程固定加载同目录引擎，
  `api` 只参与站点缓存 key）。
- `site.ext`：站点规则，三种形式均可：
  1. http(s) URL —— sidecar 用同步 HTTP 拉取（UA=Mozilla，失败重试 3 次）；
  2. 本地路径（相对本目录或绝对路径）/ `file://` URL —— 直接读文件；
  3. 规则脚本文本本身（内容里含 `var rule` 时按脚本处理）。
- `method` → 引擎调用：

| method   | 引擎调用                                                        |
|----------|-----------------------------------------------------------------|
| home     | `D.home(filter, home_html, class_parse)`                        |
| homeVod  | `D.homeVod(params)`                                             |
| category | `D.category(tid, pg, filter, extend)`                           |
| detail   | `D.detail(ids[0])`（args[0] 为数组）                            |
| search   | `D.search(wd, quick, pg)`                                       |
| play     | `D.play(flag, id, flags)`                                       |
| proxy / sniffer / isVideo | 直通引擎同名方法                                      |

stdout 每行一个响应：

```json
{"id":"1","result":{ ...catvod JSON... }}
{"id":"1","error":"错误信息"}
```

引擎返回字符串时自动 `JSON.parse`；解析失败返回 `{"raw": "<原文>"}`。

### 站点复用

同一站点以 `api + ext` 为 key 缓存规则文本，只拉取一次。**引擎内部 `rule` 等是模块级
可变单例**，因此切换站点时会用缓存文本重新 `D.init()`；所有请求在 sidecar 内**严格串行**
执行（JSONL 顺序即响应顺序），保证 “init + 方法调用” 原子，避免多站并发互相踩状态。

## 文件说明（本次交付/改动）

| 文件                  | 作用                                                                 |
|-----------------------|----------------------------------------------------------------------|
| `sidecar.mjs`         | 常驻 sidecar 主程序：全局注入、站点缓存、JSONL 分发（串行队列）       |
| `register.mjs`        | Node `module.register` 入口，加载 `loader.mjs`                       |
| `loader.mjs`          | ESM hooks：`assets://` 映射、UMD 库包装为 ESM 并补全局、定制 cheerio |
| `selectors.mjs`       | `pdfh/pdfa/pd` 宿主实现（cheerio，支持 `&&attr`、链式、双层定位）    |
| `sync_http.mjs`       | 主线程同步 HTTP 桥：`Atomics.wait` 阻塞等常驻 worker 回填            |
| `sync_http_worker.mjs`| worker 侧 async `fetch`（SAB 传元数据，临时文件传响应体）            |
| `sync_fetch.cjs`      | 旧的“每次 req fork 子进程”方案，保留为备用（当前 sidecar 不使用）     |
| `build.sh`            | 语法自检 + txbei home/category 冒烟，失败非 0 退出                   |
| `drpy2.min.js`        | drpyS 引擎（未改动）                                                 |
| `node_vendor/cheerio.min.js` | 定制 cheerio（具名导出 `jp`/`jinja2`/`load`，**非 npm 版**）   |
| `node_vendor/crypto-js.js` | CryptoJS（UMD）                                                   |
| `node-rsa.js / pako.min.js / json5.js / jinja.js / gbk.js / 模板.js` | 引擎依赖（未改动） |

## 引擎全局的宿主实现

引擎在模块 **top-level** 就引用 `pdfh/pdfa/pd`（`const defaultParser = {pdfh, pdfa, pd}`），
所以这些全局在 `import` 引擎**之前**注入：

| 全局            | 实现                                                                                  |
|-----------------|---------------------------------------------------------------------------------------|
| `req(url,obj)`  | **真同步**：常驻 1 个 Worker 跑 async fetch，主线程 `Atomics.wait` 等结果；无需每请求 fork。返回 `{content,headers,status}`，支持 method/headers/body/data/redirect/timeout（drpyS `timeout` 为毫秒） |
| `pdfh/pdfa/pd`  | 基于定制 cheerio 的选择器：`sel&&attr`、多段链式、`Text/Html/属性`、空白折叠（浏览器 innerText 风格）；`pdfa` 返回可跨调用传递的节点包装（支持 `double:true` 双层定位）；`pd` 对疑似 URL 的值做 `joinUrl` |
| `joinUrl`       | drpyS/QuickJS 宿主内置全局；引擎内部 `urljoin()` 直接包它。用 `new URL(href, base)` 实现 |
| `cheerio`       | loader 把 vendor 定制 cheerio 挂全局，并在 default 上补 `jp`（JSONPath）/`jinja2`     |
| `CryptoJS`      | vendor UMD 包装后挂全局（`md5`、`base64Decode`、AES/RSA 相关免嗅依赖它）              |
| `pako`          | UMD 包装挂全局（引擎 `gzip/ungzip`）                                                  |
| `NODERSA`       | `node-rsa.js` UMD 包装挂全局（demo/部分站点加密用；`JSEncrypt` 给 `undefined`）       |
| `JSON5`/`jinja` | UMD 包装挂全局；`jinja.render` 用于 `cheerio.jinja2`（如筛选 `{{fl.cateId}}`）        |
| `gbkTool`       | 引擎 ESM `import {gbkTool} from "./gbk.js"`（原生 ESM，直接可用，GBK 编解码）         |
| `MOBILE_UA`     | Android 移动端 UA（引擎内部还自带 `PC_UA/UC_UA/IOS_UA/UA` 常量）                      |
| `log/print`     | 写 stderr（带前缀）；`console.log` 也重定向到 stderr，stdout 只剩协议行                |
| `local`         | 进程内 `Map`：`get/set/delete(space,key)`、`getOption/setOption`                      |
| `$cache`        | 进程内 TTL 缓存：`get/set/delete`                                                     |
| `VERSION/RKEY/MY_URL/HOST` | 字符串占位；`MY_URL/HOST` 由引擎自身模块级变量驱动                          |
| `getProxyUrl/getProxy` | 返回占位代理 URL（sidecar 不内置 HTTP 代理服务）                               |
| `OcrApi`        | noop（验证码 OCR 不支持，`classification()` 返回 `""`）                              |
| `btoa/atob`     | Node 24 内置全局                                                                      |

## 验证结果（2026-10-19 实测）

**兔小贝 txbei（点播，完整链路全部打通）**
- `home`：4 个分类 `儿歌/故事/国学/启蒙`；
- `category("2",1)`：30 条列表（首条 “乖乖吃饭歌”，vod_id `2$1237@@…`）；
- `search("小猪")`：4 条（“小猪猪” 等）；
- `detail("2$1237")`：`vod_play_from=免费分享`，`vod_play_url=嗅探播放$https://www.tuxiaobei.com/play/1237`；
- `play(...)`：lazy 规则二次请求播放页解析出真实 mp4
  `https://resource-cdn.tuxiaobei.com/video/6d/c1/6dc1c72f…mp4`，`curl -I` 返回
  `HTTP/2 200 / content-type: video/mp4`。

**虎牙 huya（直播）**
- `home`：4 分类 + 完整 filters；`category("8",1)` 120 个直播间（标题/封面/人气/主播）；
  `category("1",1)` 120 个；带筛选 `extend={"cateId":"1663"}` 也正常（走 jinja2 URL 模板）。
- `homeVod`：非空（homeUrl 返回直播间）。
- **play 受限（上游规则问题，非本运行时缺陷）**：`huya.js` 的 lazy 里
  `input.match(/\/ (\d + ) / )` 正则被插入了空格（上游镜像
  `git.yylx.win/.../虎牙.js` 原文同样如此），匹配房间号必失败，回退返回播放页 URL。
  已手工验证其后的 `mp.huya.com/...profileRoom` 接口与签名逻辑依赖的
  `request/md5/base64Decode` 均正常，接口能返回带 `wsSecret` 的真实 flv/hls 地址；
  修正正则（`/\/(\d+)/`）后即可由本运行时直接产出直播流。

**斗鱼 douyu（直播）**
- `home`：9 个分类；`homeVod`：44 个直播间；`category("yl",1)`：8 条（limit=8）；
  `search("游戏")`：20 条（POST 规则）。
- `play`：规则 `lazy:""`（无免嗅），返回播放页 URL，由播放器端嗅探——这是该规则的设计行为。

> 注：txbei 的 `homeVod`（首页推荐）当前为 0 条，原因是站点首页改版后不再包含规则里的
> `.pic-list.list-box` 选择器（线上页面实测已无该 class），属规则过期；点播验收走
> category（JSON 接口）完整通过。搜索个别词（如 “儿歌”）站点本身返回 0 条，属服务端结果，
> “小猪” 等关键词有结果。

## 已知限制

1. **同步网络是单 worker 串行模型**：HTTP 请求按调用顺序执行（drpyS 脚本本身也是同步调用），
   吞吐足够爬虫场景；请求元数据帧 1MB、响应元数据帧 256KB，响应体走临时文件（无大小限制）。
2. **无真实本地 HTTP 代理**：`getProxyUrl/getProxy` 为占位；依赖引擎 `proxy()` 动态代理、
   `proxy_rule` 的站点不支持。
3. **OCR 验证码（`OcrApi`）不支持**：需要过验证码的站点会失败。
4. **存储为进程内内存**：`local`/`$cache` 不落盘，sidecar 重启即失效（无持久化 Cookie）。
5. **引擎内 QuickJS 特有 API**：以三个目标站点实际用到的为准；个别冷门 drpyS 宿主 API
   （如 JSEncrypt 前端 RSA、特殊 DOM `rr()` 序列化）给了 noop/兜底，命中冷门规则的站点
   可能报错——错误会原样进入对应响应的 `error` 字段，不影响其它请求。
6. 直播 play 的真实出流取决于各站规则自身的免嗅质量（见上文虎牙上游正则问题）。
