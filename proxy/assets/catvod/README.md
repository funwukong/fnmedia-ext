# catvod JVM 运行时（私有特性，完整版专用，公开构建剥离）

在 NAS 的 JDK 上运行 TVBox / 影视仓的 **type=3 Java 爬虫 jar**（`csp_*`），无需 Android 虚拟机。

## 适用边界

- **可运行**：普通 catvod dex jar（只含 `classes.dex`，无 native `.so`/无加固）。如小雅 `xiaoya_proxy.jar`
  （`csp_AListSh`）。这类爬虫标准实现是 okhttp+gson，Android 依赖很轻，可桩接。
- **不可运行（需 Android-x86 虚拟机）**：DexGuard 加固 jar。特征：内含
  `classes.dex` + `assets/xxx_v7.so`/`xxx_v8.so` + 加密 `*.guard`。如用户配置的全局 spider
  （伪装成 .jpg，`com.github.catvod.spider.*Guard`，真实逻辑在加密 guard + ARM so）。
  dex2jar 转出的只是壳，无法运行。

## 已验证（小雅 AListSh，2026-10-19）

`home / category / detail / play` 全链路真实数据：
- home：丫仙女、电影、电视剧三个盘分类；
- category：每页 72 条，total=362，pagecount=26059；热调用 ~0.6s（常驻 Sidecar）；
- detail：解析出集名与文件路径（115 网盘）；
- play：返回 `{parse:0, url: 小雅 /d/...?sign= 直链, header:{Authorization,User-Agent}}`，
  该 url 302 到 `cdnfhnfile.115cdn.net` 真实媒体（客户端按需播放）。

## 目录

- `jars/xiaoya_proxy.dex.jar`：从 TVBox 配置下载的原始 dex（留档）。
- `jars/xiaoya-jvm.jar`：dex2jar 转出的 JVM 字节码（运行用）。
- `lib/`：okhttp、okio、gson、kotlin-stdlib、org.json（maven）。
- `src/`：catvod 基类桩（`com.github.catvod.crawler.Spider/SpiderDebug`、`debug.MainActivity`）
  + Android 框架桩（`android/**`，TextUtils/Base64/Context/Handler/Uri/SQLite 等）
  + `Run.java`（单次 stdin-JSON）、`Sidecar.java`（常驻 JSONL）。
- `build.sh` → `build/catvod-runner.jar`。

## 重新转换新的纯 dex jar

    unzip -o spider.jar classes.dex -d /tmp/d
    d2j-dex2jar.sh -f spider.jar -o /tmp/new-jvm.jar   # dex-tools v2.4
    # 把 new-jvm.jar 放入 jars/<name>-jvm.jar；按其引用补 android/** 桩（见下）

判断缺哪些类：对 jar 内全部 class 跑 `javap -c`，grep `android/`、`com/github/catvod/` 外部引用，
在 `src/android/**` 或 `src/com/github/catvod/**` 补桩。小雅仅缺 3 个 catvod 类 + 32 个 android 类。

## Sidecar 协议（stdin/stdout JSONL）

请求每行：
    {"id":"1","class":"com.github.catvod.spider.AListSh","ext":[...驱动配置...],
     "token":"<alist JWT>","method":"category","tid":"电影","pg":"1","extend":{}}
响应每行：
    {"id":"1","result":{...catvod JSON...}}   或   {"id":"1","error":"..."}

method：home(filter) / homeVideo / category(tid,pg,filter,extend) / detail(ids[]) /
search(key,quick) / play(flag,id)。同一 (class,ext) 复用 Spider 实例（保留会话）。

classpath：`build/catvod-runner.jar:lib/*:jars/<name>-jvm.jar`，启动：
    java -Dfile.encoding=UTF-8 -Dsun.jnu.encoding=UTF-8 -cp <cp> Sidecar
（必须设这两个 encoding，否则 argv/网络中的中文乱码；本协议用 stdin JSON 规避 argv 编码。）

### 小雅登录（关键）

小雅爬虫不会自动用 ext 里的账号密码换 token（那是给 Android 弹窗分支用的）。
Runner/Sidecar 在 init 后反射：调私有 `f()` 解析 drives → 遍历 `AListSh.b`（drive 列表）
对每个 drive 调 `D(token)` 注入 alist JWT。token 由 Python 侧用
`POST {server}/api/auth/login {username,password}` 取得并随请求 `token` 字段传入（每次刷新，避免过期）。
ext 的每个 drive 形如：
    {"name":"电影","server":"http://host:port/","startPage":"/电影"}
全局项 {"type":"global"} 仅用于合并，账号不要放这里。

## 本机小雅基础设施修复（一次性，容器重建会丢）

容器 `xiaoya`（xiaoyaliu/alist，宿主端口 5678）。`/soutv` 是 nginx→busybox httpd CGI
(`/www/cgi-bin/soutv`)，在 chroot /index 里执行 shell 读索引。曾因两处损坏导致爬虫 403：

1. 容器有 `ALL_PROXY=socks5://<代理主机>:7893`，CGI 校验 token 的内部
   `curl 127.0.0.1` 被代理拦走→超时→403。修复：CGI 的 4 个校验 curl 加 `--noproxy '*'`
   （已改，备份 `/www/cgi-bin/soutv.bak.*`）。
2. chroot 缺索引 `/index/index.combined.txt` 与 `/index/dev/null`。
   修复：`cp /index/index.video.txt /index/index.combined.txt`；
   `mkdir -p /index/dev && mknod -m666 /index/dev/null c 1 3`。

验证：
    curl -X POST http://<小雅主机IP>:5678/soutv --data-binary 'echo ok'            # ok
    curl -X POST .../soutv -H "Authorization: <jwt>" \
      --data-binary "{ cat index.combined.txt;echo ''; } | grep '^[.]/电影' | head"  # 出片
