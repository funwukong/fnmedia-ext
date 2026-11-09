# fnmedia-ext · 飞牛影视在线扩展（完整功能版）

为飞牛影视（fnOS trim.media）注入在线资源能力的可逆接管插件：在线热门榜单、
海量分类深翻、纪录片 / 4K / 短剧分类库、单源独立媒体库，以及 TVBox 加固源、
小雅、DRPY/CatVod、网盘直链等完整功能，全部保留。

本仓库基座取自 [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext)，
功能架构与优化参考 [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo)，
在其基础上整合、修复并扩展（详见下方「本次更新」）。声明见 [声明.md](./声明.md)。

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

## ✨ 本次更新

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

### 保留未改动

TVBox 加固（guard/spiders）、小雅（xlib）、DRPY/CatVod、网盘（pan/dduo）、
订阅代理、播放线路竞速等全部功能与上游一致，本次未作修改。

## 📦 安装

### 方式一：FPK 包（飞牛应用中心）

1. 从 Releases 下载 `fnmedia-ext-<version>.fpk`
2. 飞牛管理后台 → 应用中心 → 安装本地包
3. 安装后打开飞牛影视，媒体库列表即出现在线库卡

### 方式二：源码部署（systemd）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo bash deploy/install.sh
```

- 管理页：`https://<飞牛地址>/v/online-admin`
- 卸载还原：`bash deploy/uninstall.sh`（自动回滚官方 socket）

## ⚙️ 媒体库开关（管理页 → 媒体库）

| 配置键 | 默认 | 作用 |
|---|---|---|
| `hot_library_enabled` | 开 | 在线热门四库（豆瓣榜单） |
| `catalog_library_enabled` | 开 | 海量分类四库 + 纪录片/4K/短剧/海外剧集/AI漫剧/TED学习 |
| `follow_enabled` | 开 | 追剧提醒（收藏的剧集数变多时提醒） |
| `source_health_enabled` | 开 | 源健康自动降级（连续失败 3 次，冷却 15 分钟内搜索跳过） |
| `domain_heal_enabled` | 开 | 域名自愈（主域名失效时按 `source_domain_backups` 自动试备用域名） |
| `metadata_enabled` | 开 | 收藏元数据补全（同名其他源补齐空字段，不改已有值） |
| `related_enabled` | 开 | 详情页关联作品（详情下方挂 recommendations，同系列/同分类优先；字段名按官方详情嵌套相关推荐数组的公开观察推断，是否渲染以真机实测为准） |
| `guard_native_enabled` | 开 | 「🛡加固影视」聚合库 |
| `guard_zone_enabled` | 关 | 每个加固源一张专区库卡 |
| `t4_library_enabled` | — | T4 远程订阅源库（未实现：本 fork v0.2.39 没有 T4 协议接入，type=4 源导入时只收录展示、不能搜不能播；上游 v0.2.66 起才有「T4 选片」聚合库） |

## 🔨 打包

```bash
export FNPACK=/path/to/fnpack
bash packaging/fpk/build.sh --fnpack "$FNPACK"
# 产物：dist/fnmedia-ext-<version>.fpk
```

## 🧪 验证

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 🙏 致谢

- [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext) —— 基座
- [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo) ——
  完整功能架构与性能优化参考
- 飞牛影视官方 trim.media —— 被扩展的宿主

## 📄 声明

项目来源、功能边界与权利归属见 [声明.md](./声明.md)。
