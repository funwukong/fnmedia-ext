# fnmedia-ext · 飞牛影视在线扩展（完整功能版）

为飞牛影视（fnOS trim.media）注入在线资源能力的可逆接管插件：在线热门榜单、
海量分类深翻、纪录片 / 4K / 短剧分类库、单源独立媒体库，以及 TVBox 加固源、
小雅、DRPY/CatVod、网盘直链等完整功能，全部保留。

本仓库基座取自 [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext)，
功能架构与优化参考 [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo)，
在其基础上整合、修复并扩展（详见下方「本次更新」）。声明见 [声明.md](./声明.md)。

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
