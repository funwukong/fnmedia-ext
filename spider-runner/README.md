# spider-runner — TVBox type3/type4 爬虫统一运行时（完整版私有，公开构建整体剥离）

把 TVBox / 影视仓配置里的非苹果CMS 爬虫源搬到本插件可调用的独立服务：
重运行时（JVM / Node / Android）放进外挂容器，主插件只通过 HTTP 调用。

## type3 与 type4 能否在插件内实现的研究结论

TVBox 站点 `type`：

- `type 0/1`：XML/JSON 苹果CMS —— 主插件原生支持，不需要本运行时。
- `type 3`（本地爬虫）：每个站点是一段 **Java/Kotlin Spider（dex/jar）** 或
  **drpyS 的 JS 规则（drpy2 引擎）**。jar 又分两类：
  - **纯 dex**：可在普通 **JVM** 上跑（d8→jar，用 catvod 基类桩）。小雅
    `csp_AListSh` 即此类，已在生产用 JVM sidecar 跑通。
  - **DexGuard 加固 jar**：`classes.dex` 只是壳，真实逻辑在加密 `*.guard` +
    `armeabi-v7a/arm64` 的 native `.so`，**只能在真 Android** 运行。
- `type 4`（服务器爬虫 / remote spider）：FongMi 现代方案本质是一个
  **Node.js 独立 bundle（`node:` 前缀）**，对每个站点暴露标准 HTTP 端点
  `/init /home /category /detail /search /play`。它不是新协议，而是把 type3
  的 JS(Spider) 搬到常驻 HTTP 服务。

**结论**：三类可以统一成**一套 catvod 协议 runner + 可插拔后端**：

| 后端   | 承载                          | 状态                       |
|--------|-------------------------------|----------------------------|
| `java` | 纯 dex（小雅 AListSh 等）     | 已上线（JVM sidecar/容器） |
| `node` | drpyS JS（童学/虎牙/斗鱼等）  | 已上线（Node sidecar/容器）|
| `android` | DexGuard 加固 jar（`*Guard` 源，ARM native） | ✅ redroid 轻量容器（NDK ARM 翻译），约0.7–0.9GB、冷启10–30秒，见 `android/README.md` |

`type4` 直接由本服务的 **`POST /node/<siteId>/<method>`** 兼容面提供（FongMi
remote spider 的 HTTP 形态），因此同一份 runner 既是 type3 的执行器，也能当
type4 的服务器端。

## HTTP 接口（app/server.py，端口 9100）

- `GET /health` → `{ok,runtimes:{java,node,android}}`
- `POST /invoke`
  ```json
  {"runtime":"java|node|android|auto",
   "class":"com.github.catvod.spider.AListSh",   // java/android
   "site":{"api","ext"},                          // node(drpy)
   "ext":..., "token":"...", "method":"home|category|detail|search|play", ...}
  ```
  `auto`：有 `class`→java、有 `site`→node。
- `POST /node/<siteId>/<method>`：FongMi type4 兼容（body 放 catvod 参数）。

## 主插件如何使用

- **默认（已上线）**：不跑容器，主插件用进程内 sidecar —— `proxy/catvod.py`
  （JVM，纯 dex）与 `proxy/drpy.py`（Node，JS）。
- **外挂容器**：`docker compose up -d runner` 起 JVM+Node 服务；主插件设置
  环境变量 **`FNM_RUNNER_URL=http://127.0.0.1:9100`** 后，`drpy.py` 自动改走
  HTTP（失败回退本地 sidecar）。`proxy/runner_client.py` 为该 HTTP 客户端。
- **加固 jar（已接入主插件）**：源定义在 `proxy/assets/guard/sites.json`，
  适配层 `proxy/guard.py`（特性 `GUARD`）。安装包不内置任何第三方加固源或 jar：用户在管理页上传自备加固 jar、按 TVBox 订阅导入并自行启用加固源。
  运行时为 **redroid 轻量容器，默认开机自启并常驻**（可手动停止），一键部署：
  `sudo bash android/setup_redroid.sh`（挂 binderfs、拉镜像、建容器、装 bridge APK、
  下发加固 jar；9110 端口直映设备 9101）。再用 `docker start/stop fnmedia-redroid`
  或管理页按钮手动启停。

## 构建与部署（本机 systemd，免容器跑 runner）

```sh
docker compose build runner                  # 或直接用部署目录 .venv 跑 server.py
# 1) runner 常驻服务（JVM+Node，android 指向 127.0.0.1:9110）
sudo install -m644 deploy/spider-runner.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now spider-runner
curl -s http://127.0.0.1:9100/health         # {"runtimes":{"java":true,"node":true,"android":true}}

# 2) Android 加固运行时（redroid；需内核 binderfs，无需 /dev/kvm；默认常驻、可手动停止）
sudo ./android/setup_redroid.sh
docker start fnmedia-redroid     # 约10–30秒就绪；stop 即释放约0.7–0.9GB

# 3) 主插件指向 runner（systemd drop-in 或环境变量）
#    FNM_RUNNER_URL=http://127.0.0.1:9100   （drpy/node、catvod 可选外挂）
#    FNM_GUARD_RUNNER_URL 缺省回退 FNM_RUNNER_URL
```

Android 后端为什么必须真实 App 进程、redroid + NDK ARM 翻译、宿主 APK 构建，
见 **[android/README.md](android/README.md)**。

## 隐私

本目录（连同 `proxy/assets/catvod`、`proxy/assets/drpy`、`proxy/assets/guard`、
`proxy/catvod.py`、`proxy/drpy.py`、`proxy/guard.py`、
`proxy/runner_client.py`、`proxy/catvod.local.json`）只存在于
完整构建；`tools/public_strip.py` 在公开发布时整体删除并做残留扫描。
