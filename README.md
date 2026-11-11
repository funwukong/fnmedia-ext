# fnmedia-ext · 飞牛影视在线扩展（完整功能版）

为飞牛影视（fnOS trim.media）注入在线资源能力的可逆接管插件：在线热门榜单、
海量分类深翻、纪录片 / 4K / 短剧分类库、单源独立媒体库，以及 TVBox 加固源、
小雅、DRPY/CatVod、网盘直链等完整功能，全部保留。

本仓库基座取自 [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext)，
功能架构与优化参考 [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo)，
在其基础上整合、修复并扩展（详见下方「本次更新」与 [CHANGELOG.md](./CHANGELOG.md)）。声明见 [声明.md](./声明.md)。

> 点播源会随时间失效，因此本扩展**不做转存/STRM 落库**——即刷即播、即搜即播；源不可用时可在管理页删除或替换。
>
> 📌 各版本更新内容见 [CHANGELOG.md](CHANGELOG.md)。

> ⚠️ **免责声明**：本项目是开源免费的技术研究 / 个人媒体库工具，**本身不提供、不存储、不分发任何影视、图片或播放地址，也不内置资源数据库**；所有在线内容均来自互联网公开的第三方接口与公开榜单，版权归原权利人所有。本项目**不提供、不推荐、不解答任何资源站点的名称、地址或获取方法**，接口由使用者自行配置并自行核实合法性。仅供学习交流与个人合法观看自有 / 已授权内容，请勿商用或传播侵权内容；由此产生的法律责任由使用者自行承担。

## 适配版本

按上游 README 标称（fork 沿用同一接管机制，未另做版本适配声明）：

| 项目 | 已验证 / 要求 |
|---|---|
| fnOS | **1.2.0 及以上**（FPK manifest 最低要求） |
| 官方影视应用 | **trim.media `0.9.8-1`**（上游已实测） |
| CPU 架构 | `all`（纯 Python，安装时创建独立 venv） |
| 安装方式 | fnOS 应用中心手动安装 FPK，可安装在 `/vol1`、`/vol2` 等数据卷 |
| 依赖 | 官方「影视 (trim.media)」必须保持安装并启用 |
| 网络 | 资源站访问使用直连（`trust_env=False`），不读取代理环境变量 |

> 本扩展通过 Unix Socket 可逆接管官方影视接口，不改官方二进制、nginx 或数据库。未来若 fnOS / trim.media 升级后签名常量或接口结构变化，停用扩展会立即恢复官方服务，等待新版本适配。

## 📦 安装

### 方式一：FPK 包（飞牛应用中心）

1. 从 Releases 下载 `fnmedia-ext-<version>.fpk`
2. 飞牛管理后台 → 应用中心 → 安装本地包
3. 安装后打开飞牛影视，媒体库列表即出现在线库卡

安装时选择应用所在存储卷，保持依赖应用 **影视 (trim.media)** 为运行状态。
安装完成后启用「飞牛影视在线扩展」，桌面/应用内会出现 **影视在线扩展管理**。

### 方式二：源码部署（systemd）

```bash
python3 -m venv.venv
.venv/bin/pip install -r requirements.txt
sudo bash deploy/install.sh
```

- 管理页：`https://<飞牛地址>/v/online-admin`；应用内 iframe 入口为 `/app/fnmedia-ext/`，旧的直连路径 `/v/online-admin` 仍保留。
- 卸载还原：`bash deploy/uninstall.sh`（自动回滚官方 socket；加 `--purge` 连运行配置 `state/` 一起删，不影响官方数据）

## 工作原理

```
浏览器/飞牛App → nginx(/v) → /var/run/trim_media.sock → fnmedia-ext(FastAPI)
├─ 普通请求 → 透传官方后端(upstream socket)
├─ /search/list → 注入在线结果（默认排最前）+ 本地结果
├─ /item /season/* /episode/* /play/* /stream/* → 伪造链路
├─ mediadb/list · mediadb/sum · item/list · item/<guid>
│ → 注入豆瓣榜单四库 + 资源站海量分类四库（无限滚动）
└─ /v/media/online/* → HLS/分片/密钥/mp4 反向代理
```

- `supervise.py` 先校验官方 socket 的监听者 exe，再在私有暂存 socket 启动本服务；
健康检查通过后用 `renameat2(NOREPLACE)` 原子换名接管；退出/崩溃时原子换回官方 socket（fail-closed）。
- 播放：HLS 源返回自研 `play_link=/v/media/online/<media_guid>/preset.m3u8`，
服务端实时抓取源站 m3u8，把分片/密钥 URI 重写为同源代理地址（透传 Referer/UA/Range）；
mp4 等非 HLS 直链返回 `/direct`，流式反向代理并透传 `Range`（支持拖动进度，206 直通）。
- 在线条目 guid = `md5("on_<provider>_<vodid>")`，media/video/season/episode guid 确定性派生、可反查。
- 标题与线路持久化在 `state/titles.json`，重启后历史卡片仍可播放。

### 热门榜单媒体库（展示 / 播放解耦，对齐 LibreTV）

“热门”不再用资源站的「最近更新」冒充（那是“刚录入了什么”，不是“大家在看什么”，且电影稀少、
海报差、无评分）。改为与 [LibreTV](https://github.com/LibreSpark/LibreTV) 相同的两层设计：

- **展示层（真实榜单）**：后台定时（默认每 6 小时）拉取豆瓣周榜 + 豆瓣官方 tag 推荐（经 60s 公开 API
免 key 转发，实例失败自动回退），合成 4 个虚拟库 **🎬热门电影 / 📺热门剧集 / 🌸动漫新番 / 🎤热门综艺**，
每库最多 160 部（豆瓣 tag 多标签多页聚合去重，综艺去重后约 130+），含豆瓣海报、评分、年份、简介；原子缓存到 `state/charts.json`，媒体库只读、秒开。
- **播放层（按需解析）**：榜单条目的 guid 是稳定的 chart guid，**点进详情时才用片名实时聚合搜索内置
资源站**（复用搜索的跨源合并与相关性过滤），命中后把可播放 brief 挂到该 guid，复用既有
详情/选集/多线路/续播/代理播放链；首次解析约几秒，之后命中内存/持久缓存秒开。源站没有的
（部分新综艺）返回 can_play=0 与「暂无在线片源」，不误导用户。
- 容错：榜单全部失败/为空时保留旧缓存；豆瓣图床经 `/online/` 海报代理并带豆瓣 Referer，
失败回退内联 SVG（HTTP 200）。兼容官方 `page/page_size` 分页、`tags.type`、`sort_column`
（榜单次序即热度，也可按评分/年份/片名排序）。
- 管理页可开关、设置榜单刷新间隔(1-24h)、立即刷新。旧的“最近更新”库 guid 仍可打开（映射到热门剧集）。
- fork 补强：并发强制刷新合并为一次（single-flight，见 `proxy/charts.py` 的 `ChartCache.refresh`），
榜单绑定持久化 chartbind：热门榜单绑定落盘，重启后直接恢复。

### 海量分类片库（方案 C · 资源站全量深翻，无限滚动）

除精选榜单外，额外注入 4 个海量库 **🎞海量电影 / 📺海量剧集 / 🌸海量动漫 / 🎤海量综艺**，用于“一直往下刷”：

- 启动/每 6 小时拉取各资源站苹果 CMS 分类表 `ac=list`，把全部子分类按名称归并到四大类，
自动剔除伦理/福利/解说/预告/体育/短剧等脏类；每类探测各源子类第一页，选页数最多的作“规范源”
（实测电影/剧/动漫可达 250 页≈**5000 部**、综艺 147 页≈2940），其余源作回退。
- 浏览时按页直连 `ac=detail&t=<子类id>&pg=N`（每页 20、详情自带播放地址，**点开即可播、无需再搜索**）；
页级 10 分钟内存缓存 + 单 flight，规范源某页失败自动换源。
- 适配官方前端**虚拟列表无限滚动**：前端按 22 条/页块请求，服务端用“全局偏移跨 CMS 页取窗 +
会话级 guid 去重 + 不足一窗自动补页”，保证滚动连续、每部片全局只出现一次、到底自然停止。
- 条目 guid 绑定 `(源, vod_id)` 并入库，复用既有详情/选集/多线路/续播/代理播放链。
- fork 补强：分类深翻有界并发 4（页顺序保持不变）；纪录片 / 4K / 短剧 / 海外剧集 / AI 漫剧 / TED 学习等
扩展分类库复用同一深翻链路（见下方「本次更新」）。

## 目录

```
proxy/
app.py 主代理：透传 + 搜索注入 + 播放伪造 + HLS/海报代理 + 管理 API
supervise.py 原子接管守护（暂存启动→校验→换名；退出自动回滚）
providers.py 苹果CMS V10 适配 + 连通性探测 + /share 播放页解析（直连国内，trust_env=False）
inject.py 在线条目 → 飞牛 item/stream/play/history 结构（标题带）
store.py 标题/线路持久化、跨源聚合、按集解析、线路/集反查索引
hot.py 「在线热门」后台刷新：跨源聚合去重、排序、原子缓存、失败保留旧缓存
charts.py 豆瓣榜单（展示层）：周榜+tag 推荐聚合、ChartCache 刷新/持久化（state/charts.json）
catalog.py 海量分类片库：CMS 分类归并、规范源选择、深翻分页
chartbind.py 榜单绑定持久化（重启后恢复）
linepredict.py 线路预测：本片最快线记忆，浏览时探活
history.py 在线播放进度（继续观看）：record 落库、play/list 合并、ts 续播
resume_diag.py 续播诊断：内存环形缓冲（最近 20 条事件），供 GET /v/api/ext/diag/resume 只读查询
config.py 可热加载配置：自定义源增删、启用开关、噪音过滤、在线前排；保存时备份最近 5 份
admin_ui.py 单文件管理页（源管理/测速/设置）
fnmedia_authx.py 官方 authx 签名客户端（MD5 + apiKey）
vlibs.py 虚拟库：显隐/排序/位置/子账号授权（上游设计，见「多用户与权限」）
fnusers.py 身份识别：飞牛官方 SQLite 只读查 token→用户（30 秒缓存，不写不锁）
libfilter.py 按库过滤：搜索/收藏/播放链按虚拟库可见性过滤
t4.py T4 远程订阅源（type=4，见「远程 T4 接入」）
drpy.py DRPY JS 爬虫桥接（默认 drpy3 Runtime，drpy2 作 fallback）
netutil.py 取源代理：三档代理模式（off/on/auto）
sps.py 去广告指纹（H.264 分片头识别无标记重编码广告）
catvod.py 轻量猫源：11 站纯苹果 CMS，零运行时
deploy/ 持久 systemd 单元 + install/uninstall
packaging/fpk/ 飞牛应用中心 FPK 打包
wheelhouse/ 离线安装 wheel（20 个，无网 NAS 也能装好）
state/ titles.json / config.json / charts.json / history.json（运行时生成）
```

## 使用

- 打开飞牛影视，搜索任意**中文片名**（拼音上屏后才触发在线搜索，纯拼音碎片只走本地）。
- 标题带 **** 前缀、系列列显示「🌐在线·年份·地区」的卡片，点开即播；在线结果默认排在最前。
- 管理页（同源）：`https://<你的飞牛地址>/v/online-admin`
- **资源站管理**：启用/停用、添加自定义苹果 CMS JSON 接口、删除（误删可用「恢复默认」还原出厂线路）
- **连通性测试**：单源测试或「全部测速」，显示耗时、命中条数、直链数，方便筛掉失效源
- 每词注入条数、在线前排开关
- **噪音关键词自定义**：标签式增删、批量粘贴（逗号/顿号/换行分隔）、一键恢复默认；
标题或备注命中任一关键词即不注入
- **热门榜单媒体库**：开关、榜单刷新间隔、立即刷新；查看最近刷新时间、数据源成功数、
各库条数与错误信息；展示豆瓣电影/剧集/动漫/综艺四个榜单，点击按需搜源播放
- **虚拟媒体库**：每库隐藏开关 + ↑↓ 排序、虚拟库位置（本地库之前/之后/插到指定本地库之后）、
子账号列表按人勾选允许的库（含“允许在线搜索”标记）与未配置账号的默认开关
- **T4 订阅**：在「导入订阅」中粘贴包含 `sites` 数组的统一订阅（见「远程 T4 接入」）

## 资源站接口格式

添加的自定义源需为苹果 CMS V10 标准 JSON 接口，形如：

```
https://站点/api.php/provide/vod/at/json # 搜索?ac=detail&wd=关键词
```

返回 `{"code":1,"list":[{vod_name, vod_play_url,...}]}` 即被识别。
fork 另支持 XML 订阅源（`parse_cms_xml` 解析为同一结构，`/at/xml` 搜索/探测分流，
`add_source` 入库自动归一 `/at/xml`→`/at/json`）。

## 管理 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v/api/ext/status` | 运行状态 + 配置（不含 token）|
| GET | `/v/api/ext/online?q=&limit=` | 已缓存在线条目 |
| POST | `/v/api/ext/config` | 保存设置/源启用状态（热生效，清搜索缓存）|
| POST | `/v/api/ext/junk-keywords` | `{keywords}` 自定义噪音词（自动分隔去重）或 `{reset:true}` 恢复默认 |
| POST | `/v/api/ext/source/add` | `{name,api}` 添加自定义源（按地址去重）|
| POST | `/v/api/ext/source/delete` | `{id}` 删除/隐藏一条线路（默认线路可恢复）|
| POST | `/v/api/ext/source/restore` | 恢复全部默认线路 |
| POST | `/v/api/ext/source/test` | `{id}` 或 `{api}` 单源连通性探测 |
| POST | `/v/api/ext/source/test-all` | 并发探测全部源 |
| POST | `/v/api/ext/source/update` | 更新一条源（管理页源编辑弹窗调用）|
| POST | `/v/api/ext/hot/refresh` | 立即刷新豆瓣榜单缓存（失败保留旧缓存）|
| GET | `/v/api/ext/diag/resume` | 续播诊断：内存最近 20 条事件 + 按分区查记录（只读，见「播放记录 / 继续观看」）|
| GET | `/v/api/ext/accounts` | 子账号列表（身份来自飞牛官方 SQLite 只读识别）|
| POST | `/v/api/ext/account/acl` | 设置某账号允许的虚拟库（含 `__online__` 标记）|
| POST | `/v/api/ext/account/defaults` | 未配置账号的默认可见/在线搜索开关 |
| GET | `/v/api/ext/libs` | 虚拟库列表（显隐/排序/位置）|
| POST | `/v/api/ext/lib/visibility` | 隐藏/显示某虚拟库 |
| POST | `/v/api/ext/lib/order` | 虚拟库排序与位置 |
| GET | `/v/api/ext/cache` | 缓存状态 |
| POST | `/v/api/ext/cache/clear` | 一键清缓存（含榜单缓存）|
| GET | `/v/api/ext/source-health` | 源健康状态 |
| POST | `/v/api/ext/source-health/reset` | 重置源健康计数 |
| POST | `/v/api/ext/source/heal` | 域名自愈：按备用域名重试 |
| GET | `/v/api/ext/follow` | 追剧提醒列表 |
| POST | `/v/api/ext/follow/check` | 检查收藏剧集是否有更新 |
| GET | `/v/api/ext/continue` | 继续观看（在线历史）|
| GET | `/v/online-admin` | 管理页 |

> 注意：`/v/api/ext/*` 写端点目前无鉴权门（已明确决定不加，见 CHANGELOG v1.1.0「未动」）。

## 选集（原生 TV UI）

- 单集条目渲染为 **Movie**（点开即播）；多集条目渲染为 **TV**。
- TV 走飞牛原生选集接口：`GET /season/list/:tvGuid` 返回一个伪造 Season，
`GET /episode/list/:seasonGuid` 返回每一集（确定性 `episode_guid=md5(ep:on:idx)`，
集身份与播放线路解耦，切线路不换集）。
- 点某集 → `play/info{item_guid:episodeGuid}` → `play/play`，media/video guid 与该集绑定，
播放器拉到的就是该集 HLS。媒体 guid 反查索引在 store 内存中维护，随搜索结果增量重建。

## 多线路切换

- 搜索时把**不同资源站命中的同一部片聚合成一个条目**（片名规范化 + 年份归并），
每个资源站各成一条播放线路（线路键 `provider::组名`，同一站直链优先于 share 页，
直链最多的线路为默认）。
- 播放设置里的**清晰度菜单即「线路」菜单**：`POST /stream` 与 `GET /stream/list`
返回的 `qualities` 为每个可用线路生成一项（线路 1、线路 2……，不内置任何站点名称展示）。播放器切换该项时
会重新调用 `POST /play/play`，代理用请求里的线路码（编码在 `bitrate` 字段）解析
对应源，返回只含该线路的播放列表，从而在卡顿/失效时即时换源。
- 标题播放键对 TV 也可直接点击（`can_play=1`）：从未看过播第 1 集；有未看完的集则
续播该集并恢复到上次秒数；已看完的集之后自动播下一集。
- 嵌套 master（源 `index.m3u8` 再套一层子 m3u8）会自动跟随解析（最多 4 层），
并正确重写 `EXT-X-KEY`（AES）与 `EXT-X-MAP`（fMP4 初始化段）。
- `/share/<id>` 等播放页源在**播放时按需解析**（页面内嵌 `const url=...m3u8` 提取，
30 分钟缓存），不拖慢搜索；直链线路始终优先于仅可解析的线路。
- fork 补强：二级线路海报墙——媒体库一级改为影片文件夹，点进影片后按资源站聚合展示线路海报
（解析线 + m3u8 直放线合成一张卡，优先直放线）；POST `stream/list` 与 GET 共用实现，网页端多集选集恢复；
线路身份贯穿播放链路，选集不串线。线路预测 linepredict：本片最快线记忆，浏览时探活。
播放线路竞速（`_race_fastest` 与四个打分常量）**冻结，零改动**。

## 播放记录 / 继续观看

- 在线播放进度（`POST /play/record`，播放器高频上报）落在本地 `state/history.json`，
不上报官方库。
- 代理 `GET /play/list` 时把在线历史（带海报、剧名、季集、`ts` 进度、`duration`）
与官方本地历史合并返回，因此在线条目出现在「继续观看」，置顶按最近播放排序；
进度 ≥95% 视为看完不再占位，`DELETE /play/record` 同步删除。
- `POST /play/info` 回填已记录的 `ts`（秒），播放器据此从上次位置续播。
- fork 续播修复（v1.1.0，三轮，详见 CHANGELOG）：
文件夹/季卡 guid 归一化到标题；`play/record` 守门认标题/集/video/media 任一 guid；
真实 duration——CMS 的 `vod_duration`（兼容 `vod_length`/`duration`）取不到时从 HLS 清单 EXTINF 求和兜底；
同一集的历史身份统一为“归一化标题 + 季 + 集号”，换线路也能接上，旧记录读时迁移、不丢数据。
**以上续播修复尚未在真机上验证通过，待真机验证。**
- 续播诊断端点 `GET /v/api/ext/diag/resume`（只读）：内存最近 20 条续播事件
（`record` 事件带原始/归一化 guid、解析结果、分区、ts/duration、规范键；
`info` 事件带 duration），加按分区查记录（`?guid=`）；不记任何 token / 凭证原文，只记 guid 与分区键。
排查续播丢位置时先取该端点 JSON：record 与 play/info 的分区不一致是首要嫌疑。
「客户端要合法时长才肯续播」仍是待证伪的假设，以诊断 JSON 为准。

## 多用户与权限（上游设计 vlibs + fnusers）

fork v0.2.42 起改走上游 yujian0815/fnmedia-ext 的 vlibs + fnusers 设计（删掉了自定义的
`identity.py` / `visibility.py` 与配置键 `library_visible_users`）：

- 虚拟库按稳定库键（`chart:movie`、`catalog:shortdrama`、`t4` 等）配置显隐/排序/授权；
管理员全局隐藏的库（`lib_hidden`）所有账号都看不到，管理页可重新打开。
- 身份从飞牛官方 SQLite 只读识别（`trimactivity.db.user_token` → `trimmedia.db.user`，
正缓存 30 秒、负缓存 10 秒、缓存上限 2000 条，不写库不加锁）；识别不到时按默认开关处理。
- **默认 fail-open（上游原样）**：`acl_default_visible` / `acl_default_online` 默认均为 `True`，
未配置账号默认能看到虚拟库、能在线搜索；管理员与识别不到的会话始终放行。
v1.1.0 曾把两项默认改成 fail-closed，后改回 fail-open：已在真机跑过 v1→v2 迁移、两项已落盘为 `False`
的存量配置，首次加载经 v2→v3 迁移一次性翻回 `True` 并写回版本 3，之后手动改回 `False` 会保留。
想改回隐藏：管理页“子账号可见权限”里关掉“未设置过权限的账号默认能看到虚拟库”，
或把配置 `acl_default_visible` 改为 `false`。
- 搜索在线开关同步上游：未配置账号按 `acl_default_online`，已配置账号按其 `__online__` 标记决定能否搜在线片源。
- 播放历史按飞牛账号隔离：各账号只看自己的进度与续播；收藏按账号分区存储。
- 搜索与过滤按库执行：搜索早返回按**可见**数算（≥25 条且 ≥0.8 秒），受限账号不再因早返回系统性变空；
收藏/继续观看/播放链按库过滤（play/info、stream、play/play、分片代理、T4、红果均加库检查，沿用 -6 未授权模式）；
已配置白名单的账号照样只看白名单。

## 远程 T4 接入

T4 爬虫运行在外部服务端，扩展只接收标准 JSON 结果并对接飞牛原生目录与播放器；
不在 NAS 扩展内运行 Android/JAR/Python/JS 爬虫，不捆绑私有插件或账号。

1. 在管理页 `/v/online-admin` 的「导入订阅」中粘贴包含 `sites` 数组的统一订阅。
2. 所有 `type=4` HTTP(S) 来源均可登记，保留 `ext`、GET/POST 和搜索开关；
同地址与 `ext` 去重，不设固定插件数量白名单。首页测试成功不等于影片可播。
3. 回到原生搜索，或打开自动出现的「T4 选片」，选择插件、分类、筛选和目录。
首页/分类不支持的纯搜索来源仍走搜索入口；来源同名不会合并身份。
4. 选中影片才加载详情，明确季集映射到原生选集，同集切线不按文件下标猜测；
按播放请求取链。收藏/登录等 action 卡片不伪装成影片。

T4 选片继承已有媒体库隐藏、排序和子账号权限；原生搜索沿用在线搜索授权，
历史沿用上游分用户记录。新增详情仍使用已有防抖写盘和缓存上限，不在搜索热路径同步写盘。
普通 T4 不依赖 Sidecar：只有导航卡片的 `t4_action.type=search`、增量搜源和持久内容身份等
扩展协议需要相应服务端支持。

### 媒体交付与性能

- 公网无头直链交给客户端；带头或私网媒体按需流式代理，支持 Range/HEAD、
嵌套 HLS、音轨、分片和密钥请求。头信息不写入最终播放地址或历史。
- 容器类型以最多 4 KiB 实际字节识别，不凭扩展名伪报 MP4/HLS。可选容器元数据
最多读取 8 MiB、限时 8 秒、最多两路；只使用剩余起播预算，超时后台补齐并回收。
- 搜索最多 8 路，T4/CMS 交替调度，有 T4 时最多 5 秒收集已完成结果并取消剩余请求；
浏览最多 4 路、列表缓存 60 秒且限制体积，详情/取链请求合并。
- 不解码、不转码、不重封装，不修改媒体字节；客户端仍须支持实际容器和编码。
日志记录客户端、阶段与耗时，播放参数脱敏。
- HTTPS 反代须经扩展所在的 `/v` 入口，传递完整 `X-Forwarded-Host`（含端口）和
`X-Forwarded-Proto`；不应直连绕过 Socket 扩展的官方后端。原文件返回完整地址，
不要求原生客户端补全相对路径，不另设 Gateway Token。

### 适配与验证

支持 T4 首页、分类/筛选、搜索、详情、多季多线路、HLS/MP4/MKV 网络交付。
上游须完成客户端嗅探/解析任务；DASH/双轨、外挂字幕、直播和 AT action 写回不在本次承诺范围。
标准 T4 可独立使用，AT 特殊能力是否可用由其 T4 服务端实现决定。
既有 fork 的 NAS 验证不是本贡献分支的宿主部署验收；UA 回归与 HTTP 200/206 也不等于
TV/Android/iOS 真机解码或长播通过。当前 HEVC 浏览器样本不支持，需匹配客户端或更换线路。

运行协议与合成媒体回归：

```bash
python -m pytest tests/ -q
python -m compileall -q proxy tests
```

测试使用临时状态、Mock 接口和回环 HTTP，不读取生产凭证、不接管官方 Socket。
可选媒体夹具缺少 `ffmpeg`/`ffprobe` 时跳过，运行服务不要求它们进行视频转码。

### DRPY / CatVod（fork）

- DRPY JS 爬虫由常驻 Node sidecar 执行（`proxy/drpy.py`，JSONL 通信）；
引擎默认 **drpy3** Runtime（每站独立实例），`DRPY_ENGINE=drpy2` 切回老路 drpy2.min.js 单例作 fallback。
- CatVod：轻量猫源 11 站纯苹果 CMS，零运行时。
- TVBox 加固源（guard/spiders）、小雅（xlib）、网盘（pan/dduo）沿用上游/既有实现。

## ✨ 本次更新

> v0.2.42 / v1.1.0 的完整更新见 [CHANGELOG.md](./CHANGELOG.md)。以下为更早一版的更新记录。

### 新增分类库（热门/海量媒体库区）

| 库卡 | 说明 | 开关 |
|---|---|---|
| 🎞 纪录片 | 资源站「纪录 / 记录」类目聚合深翻 | 随「海量四库」主开关 |
| 📽 4K影视 | 资源站 4K / 杜比 / 蓝光 / 超高清类目聚合 | 随「海量四库」主开关 |
| 📱 热门短剧 | 资源站短剧 / 微短剧类目聚合深翻 | 随「海量四库」主开关 |

- 纪录片 / 4K / 短剧不设独立开关，跟随「海量片库」主开关一起显示。
- 4K 库两条来源：资源站类目名含 4K/杜比/蓝光/超高清时按类目建库；源没有
4K 类目时，按片名/备注的画质信号（4K/2160/UHD/杜比/HDR/蓝光/超高清）
派生——浏览电影/剧集时 4K 片自动抄进 4K 库，打开 4K 库则借电影分类翻页
过滤；“超清”不算 4K 信号。路由刷新后会后台预热一次 4K 列表（库卡才有
海报）；源备注里完全没有 4K 信号时库就是空的，总数报 0，不会虚报条数
让前端一直翻空页。

### 修复

- **TV 端海报显示**：条目与库卡海报统一改用官方图片代理前缀
`/v/api/v1/sys/img/online/...`，修复 TV/安卓 App 海报留白裂图
（裸 `/online/` 路径图片加载器不可达）。
- **连续切换开关后卡片不恢复**：路由刷新改为「等当前轮结束按最新配置补跑」，
不再丢失切换事件。
- **配置保存中文/emoji 崩溃**：`state/config.json` 读写强制 UTF-8
（修复 Windows 默认 GBK 编码 `UnicodeEncodeError`）。
- **纪录片归类遗漏**：资源站类目错别字「记录片」同样归入纪录片库
（纪录片路由 2 → 8）。

### P0/P1 优化要点（v1.1.0，详见 CHANGELOG）

- 搜索：早返回按可见数算（≥25 条、≥0.8 秒）、聚合缓存合并为单层（TTL 180 秒）、
每请求 ViewerContext（配置深拷贝从约 70–100 次降到个位数）、搜索级判死阈值对齐上游 v0.2.71（5 次 / 3 分钟）。
- 慢线：`main_ready_budget` 默认 2.4 秒（夹取 [0.8, 2.8]），管理页 POST 真正生效、改值无需重启；
慢线 future 异常取回并记日志/计数。
- 写盘与缓存：标题存储改脏跟踪写盘（不再每 5 秒静默期满全量写 MB 级 `titles.json`）；
`titles.json` 防抖落盘 + 4000 条/4MB 上限；fnusers 锁外查库。
- 海报重试收敛：网络异常最坏从 7 次 GET 降到 ≤3 次；403 带图床自身 origin 作 Referer 的重试保留。
- 配置：保存时备份最近 5 份（`state/backups/config-<时间戳>.json`，主配置损坏时按时间倒序恢复）；
CONFIG_VERSION 稳健解析（接受 int、整数 float、`"2"`、`"2.0"`，不可解析时跳过破坏性迁移）；
死配置开关接线（`source_health_enabled` / `domain_heal_enabled` 门控对应调用点）。
- 订阅与网盘：XML 订阅源解析；网盘过期转存清理（每小时、TTL 6 小时，只删带可信时间戳的过期文件；
删除端点形状按夸克/UC 网页端惯例推断，**需真机 + 真实网盘账号验证**）。
- 管理页与 CI：资源站分页（20/页）+ 筛选、开关失败回滚状态；Release tag 派生修正；
全量测试 766 通过（v0.2.42 时 373）。

## ⚙️ 媒体库开关（管理页 → 媒体库）

| 配置键 | 默认 | 作用 |
|---|---|---|
| `hot_library_enabled` | 开 | 在线热门四库（豆瓣榜单） |
| `catalog_library_enabled` | 开 | 海量分类四库 + 纪录片/4K/短剧/海外剧集/AI漫剧/TED学习 |
| `follow_enabled` | 开 | 追剧提醒（收藏的剧集数变多时提醒） |
| `source_health_enabled` | 开 | 源健康自动降级（连续失败 5 次，冷却 3 分钟内搜索跳过） |
| `domain_heal_enabled` | 开 | 域名自愈（主域名失效时按 `source_domain_backups` 自动试备用域名） |
| `metadata_enabled` | 开 | 收藏元数据补全（同名其他源补齐空字段，不改已有值） |
| `related_enabled` | 开 | 详情页关联作品（详情下方挂 recommendations，同系列/同分类优先；字段名按官方详情嵌套相关推荐数组的公开观察推断，是否渲染以真机实测为准） |
| `lib_hidden` | `[]` | 管理员全局隐藏的虚拟库（按稳定库键，如 `chart:movie`）：隐藏后所有账号都看不到该库，管理页可重新打开。 |
| `lib_order` / `lib_placement` / `lib_anchor` | `[]` / `front` / `""` | 虚拟库自定义顺序与相对本地库的位置（front=本地库之前，back=之后，after=插到指定本地库之后，锚点为本地库 guid）。 |
| `lib_acl` | `{}` | 子账号可见授权：{用户 guid: [允许的库键，含 `__online__` 标记]}。未配置的账号走默认（见下两行）。身份从飞牛官方 SQLite 只读识别（token→用户，30 秒缓存）；识别不到时按默认处理。 |
| `acl_default_visible` | 开 | 未配置账号默认能否看到虚拟库（上游 fail-open 默认；关掉后未配置账号默认看不到虚拟库，管理员与识别不到的会话仍可见）。 |
| `acl_default_online` | 开 | 未配置账号默认能否用在线搜索（关掉后未配置账号只搜本地库；已配置账号按其 `__online__` 标记决定）。 |
| `guard_native_enabled` | 开 | 「🛡加固影视」聚合库 |
| `guard_zone_enabled` | 关 | 每个加固源一张专区库卡 |
| `t4_library_enabled` | — | T4 远程订阅源库（已接入：type=4 源导入后进入「T4 选片」聚合库，可搜可播；开关控制是否在媒体库中显示该库） |

## 🔨 打包

```bash
export FNPACK=/path/to/fnpack
bash packaging/fpk/build.sh --fnpack "$FNPACK"
# 产物：dist/fnmedia-ext-<version>.fpk
```

- `deploy/fnmedia-ext.service.in` 是 systemd 模板，安装时 `deploy/install.sh` 按
实际落盘路径渲染绝对路径，因此同一包可装到任意目录。
- `cmd/install_callback` 在首次安装时创建 venv 并 `pip install fastapi uvicorn httpx`，
随后调用 `deploy/install.sh` 接管；`uninstall_callback` 调用 `deploy/uninstall.sh` 还原。

## 🧪 验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 已知限制 / 待真机验证

- `/share/<id>` 解析器目前覆盖内嵌 `const/var url` / `{"url":...}` / 明文 m3u8
的播放页（苹果系常见）；个别用加密 JS 动态拉流的页面可能解析不到（自动跳过该线路）。
- 多线路聚合依赖「片名规范化 + 年份」判定，极少数同名不同作品理论上可能被并为一条。
- 已在 fnOS 应用中心完成全新安装 / 停用 / 启用验证（上游）；适配版本以本文顶部「适配版本」表为准。
官方 trim.media 升级若改动签名常量或接口结构，需要相应更新本扩展；停用即可恢复官方服务。
- fork 待真机验证（均已在 CHANGELOG 标注）：续播三轮修复（跨线续播、真实 duration、诊断端点本身可用，
但“修好”以真机诊断 JSON 为准）；网盘过期转存清理的删除端点形状；4K 库无图（可能与备注缺少
4K/2160/杜比/HDR/蓝光画质信号有关）；收藏跨源碎裂与跨形态取消收藏缺口未修；`/v/api/ext/*` 写端点无鉴权门是既定现状。
- Turbo 播放竞速冻结（`_race_fastest` 与四个打分常量零改动）；冻结五项（固定线直出、零竞速秒播、
播放中静默换线、浏览时探活、海报角标）未搬入。

## 🙏 致谢

- [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext) —— 基座
- [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo) ——
完整功能架构与性能优化参考
- 飞牛影视官方 trim.media —— 被扩展的宿主

## 📄 声明

项目来源、功能边界与权利归属见 [声明.md](./声明.md)。
