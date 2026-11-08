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
- 4K 库按资源站类目名归并建库：当前资源站类目中无 4K/高清类命名时，
  路由为空、卡片自动隐藏；资源站出现对应类目后卡片自动出现。

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
| `catalog_library_enabled` | 开 | 海量分类四库 + 纪录片/4K/短剧 |
| `guard_native_enabled` | 开 | 「🛡加固影视」聚合库 |
| `guard_zone_enabled` | 关 | 每个加固源一张专区库卡 |
| `t4_library_enabled` | 开 | T4 远程订阅源库 |

## 🔨 打包

```bash
export FNPACK=/path/to/fnpack
bash packaging/fpk/build.sh --fnpack "$FNPACK"
# 产物：dist/fnmedia-ext-<version>.fpk
```

## 🧪 验证

```bash
python -m pytest tests/test_ingress.py tests/test_t4_browse.py -q
```

## 🙏 致谢

- [yujian0815/fnmedia-ext](https://github.com/yujian0815/fnmedia-ext) —— 基座
- [cjm2004/fnmedia-ext-turbo](https://github.com/cjm2004/fnmedia-ext-turbo) ——
  完整功能架构与性能优化参考
- 飞牛影视官方 trim.media —— 被扩展的宿主

## 📄 声明

项目来源、功能边界与权利归属见 [声明.md](./声明.md)。
