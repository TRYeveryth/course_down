# course-tools

使用 Python 和 Playwright，通过学习平台页面的官方“下载”入口，按课程目录顺序保存 PDF、MP4 和 PNG/JPEG 附件。支持目录翻页、已完成文件跳过、失败课程重试及离线文件核验。

当前适配云学堂一类的 `#/list`、`#/course/play`、`#/scan` 页面和“文档 / 视频”课程大纲。**填写自己的网址是使用前提；仅更换网址不代表兼容任意学习网站。** 页面结构或接口不同的部署需要调整适配代码。

仓库不包含真实课程网址、登录状态或课程内容。请使用自己有权访问并下载的课程。

## 项目结构

```text
course-tools/
├── config.example.py       # 用户配置模板：复制为 config.py 后填写
├── courses.example.txt     # 用户课程清单模板
├── requirements.txt        # 下载依赖
├── requirements-verify.txt # 可选文件核验依赖
├── scripts/                # 所有运行代码及 Windows 辅助脚本
│   ├── __init__.py         # Python 模块入口声明
│   ├── download_catalog.py # 入口：遍历目录分页，依次下载课程
│   ├── download_official.py # 入口：下载单个课程或指定课程清单
│   ├── retry_failed.py     # 入口：列出并重试失败课程
│   ├── verify_downloads.py # 入口：离线核验已保存文件
│   ├── probe.py            # 入口：交互式探测页面资源
│   ├── common.py           # 配置、网址校验、浏览器连接、文件名和运行锁
│   ├── transfer.py         # 官方接口解析及文件保存
│   ├── run.ps1             # Windows 统一运行入口
│   ├── start_chrome_cdp.ps1
│   └── start_chrome_cdp.cmd
├── .gitignore
├── LICENSE                 # MIT
└── README.md
```

运行时生成的 `output/`、`.venv/`、`profile/`、`chrome-profile/`，以及用户填写的 `config.py`、`courses.txt` 均已加入 Git 忽略规则。

所有运行代码位于 `scripts/`，日常下载使用其中的入口模块。

以下命令均在仓库根目录运行，使用 `python -m scripts.<模块名>`。配置文件及下载目录仍位于根目录，原先的 `python download_catalog.py` 等命令需改为新的模块命令。

## 1. 环境与依赖

主要在 Windows 上使用 Python 3.11 / 3.12 和已安装的 Google Chrome 验证。脚本直接使用本机浏览器，无需额外安装 Playwright 的 Chromium 包。

| 依赖 | 用途 | 是否必须 |
|---|---|---|
| Python 3.11 或更新版本 | 运行脚本 | 必须 |
| Playwright ≥ 1.62，< 2 | 浏览器操作及 CDP 连接 | 必须 |
| Requests ≥ 2.32，< 3 | 交互式资源探测的下载功能 | 安装基础依赖时包含 |
| Google Chrome | 连接已有窗口 | 推荐下载方式需要 |
| Microsoft Edge | `scripts/probe.py` 和不使用 CDP 的单课程模式 | 使用这两种模式时需要 |
| pypdf、Pillow | PDF 可读性和图片完整性核验 | 核验需要 |
| ffprobe（通常随 FFmpeg 安装） | 视频轨和时长核验 | 核验 MP4 时需要 |

克隆或解压仓库后进入 `course-tools`，选择一种 Python 环境。

### Miniconda / Anaconda

```powershell
conda create -n course_tools python=3.11 -y
conda activate course_tools
python -m pip install -r requirements.txt
```

已有环境可以直接激活后安装依赖，无需再次创建。

### Python venv

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

也可以直接使用 `.\.venv\Scripts\python.exe` 代替命令中的 `python`。

核验下载内容时，额外安装：

```powershell
python -m pip install -r requirements-verify.txt
ffprobe -version
```

第二条命令用于确认视频核验工具可通过系统 `PATH` 找到；安装 Python 包不会自动安装 ffprobe。

## 2. 填写自己的网址

首次使用时复制配置模板：

```powershell
Copy-Item config.example.py config.py
```

编辑 `config.py`，把浏览器地址栏中的完整链接粘贴到变量中，保留 `#` 后的路由和查询参数：

```python
CATALOG_URL = ""   # 填写要遍历的课程目录链接
COURSE_URL = ""    # 可选：填写单个课程详情链接
SITE_ORIGIN = ""   # 可选：只填写 HTTPS 站点源；通常由上面链接自动推导
CDP_URL = "http://127.0.0.1:9222"  # 本机 Chrome 的实际调试地址
API_HOST_SUFFIXES = ("yunxuetang.cn", "yxt.com")
FOLDER_SUFFIX = "" # 留空使用运行当月 YY-MM；续传跨月时可填固定后缀
```

- 批量下载需要 `CATALOG_URL`。目录链接应包含平台的 `#/list` 路由和 `catalogId`。
- 单课程下载可填写 `COURSE_URL`，或在运行时用 `--course-url` 指定。
- 同一轮下载只允许配置站点中的课程链接。切换到另一个站点时，请同步更新或清空旧的站点配置。
- `API_HOST_SUFFIXES` 是允许解析的官方 API 域名范围，不含协议和路径。平台 API 部署在其他域名时，需要按实际请求修改。
- `CDP_URL` 是本机调试服务地址，课程网站地址填在前两个变量中。

也支持用 `--catalog-url` / `--course-url` 覆盖默认链接。终端中的完整网址请加引号。

## 3. 准备已登录的浏览器

推荐连接已打开的 Chrome，让脚本复用其登录上下文，并在新标签页操作。

如果 Chrome 提供内置远程调试开关，可在 `chrome://inspect/#remote-debugging` 开启，并把显示的本机地址填入 `CDP_URL`。脚本连接时，Chrome 若弹出调试授权提示，需要在浏览器中点击“允许”。

另一种方式是在 Windows 中运行：

```powershell
.\scripts\start_chrome_cdp.cmd
```

它读取配置中的本机调试端口和起始网址，使用仓库内的独立 `chrome-profile/` 启动 Chrome。在这个窗口中完成平台登录；未填写起始网址时打开空白页。

专用配置目录用于满足 Chrome 命令行调试的配置隔离要求，详见 [Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)。浏览器连接使用 [Playwright 的 CDP 接口](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)。

结束连接模式的脚本时，只断开自动化连接，原 Chrome 窗口及标签页会保留。同一时间运行一份下载脚本，运行锁会拒绝重复启动。

## 4. 遍历全部分页并下载

先检查课程清单：

```powershell
python -u -m scripts.download_catalog --inspect-only
```

确认识别的清单与网页一致后下载：

```powershell
python -u -m scripts.download_catalog --yes
```

脚本会读取接口中的课程总数，点击“下一页”并等待列表变化，按页码、每页从左到右再从上到下的顺序采集课程。数量不一致时停止，不按残缺清单下载。采集完成后复用 `scripts/download_official.py`，依次进入课程，按左侧大纲下载全部章节；独立 PDF 和视频也由目录中的类型信息识别。

`--inspect-only` 不下载文件，但会打开页面并点击独立视频卡片以确认真实地址。临时探测标签页会关闭，原目录页保持当前页码。

| 参数 | 含义 | 默认值 |
|---|---|---|
| `--catalog-url` | 覆盖配置中的目录链接 | `CATALOG_URL` |
| `--cdp` | 覆盖本机调试地址 | `CDP_URL` |
| `--inspect-only` | 只采集和保存清单 | 关闭 |
| `--yes` | 跳过清单确认，直接下载 | 关闭 |
| `--first-page` | 只处理第一页 | 关闭，默认遍历全部分页 |
| `--expected-count N` | 额外核对目录总项数 | 自动读取接口总数 |
| `--catalog-timeout N` | 目录采集总等待时间 | 240 秒 |
| `--ready-timeout N` | 等待课程资源和下载入口 | 60 秒 |
| `--timeout N` | 点击下载后等待文件事件或接口结果 | 45 秒 |

网络较慢时：

```powershell
python -u -m scripts.download_catalog --yes --ready-timeout 120 --timeout 90
```

`--all-pages` 是兼容旧命令的参数，全部分页现在已经是默认行为。

## 5. 下载单个课程或指定清单

填写 `COURSE_URL` 后，先在新标签页检查大纲：

```powershell
python -u -m scripts.download_official --cdp --inspect-only
python -u -m scripts.download_official --cdp --yes
```

这里 `--cdp` 不带值时使用配置中的 `CDP_URL`。也可以传入本机地址，或使用 `--course-url` 指定另一个同站点的课程详情页。

指定多个课程时，复制清单模板并逐行填写自己的课程链接：

```powershell
Copy-Item courses.example.txt courses.txt
python -u -m scripts.download_official --cdp --courses courses.txt --yes
```

清单模式保留文件中的顺序。单课程和手工清单模式用于有左侧大纲的课程详情页；独立 PDF / 视频请通过目录脚本处理，以获得文件类型等元数据。

省略 `--cdp` 会启动脚本自己的 Edge 窗口，需要在那里登录；退出时该窗口关闭。未使用 `--yes` 时，脚本会等待人工准备页面并确认章节清单。

## 6. 列出并重试失败项

需要先保留批量下载生成的课程清单和下载记录：

```powershell
python -u -m scripts.retry_failed --list-only
python -u -m scripts.retry_failed
```

脚本从失败记录匹配原目录课程，只重试相关课程。成功且文件大小一致的章节会跳过；首次失败后尚未处理的后续章节会继续下载。默认入口等待 120 秒，下载事件等待 90 秒，可用同名参数调整。

中断的重试批次会保存在 `retry-pending-courses.json`，再次执行同一命令即可继续。首个失败文件下载成功后，课程剩余章节也会保留在重试范围内。

## 7. 核验下载结果

完成批量下载后，安装可选依赖并确保 `ffprobe` 在 `PATH` 中：

```powershell
python -u -m scripts.verify_downloads
```

核验使用课程清单和下载记录，检查文件路径、记录大小、PDF 页面可读性、MP4 视频轨与有效时长、PNG/JPEG 图像结构，同时检查失败记录及未结束的重试批次。视频核验不进行逐帧解码。

可通过 `--output` 指定另一个下载目录。只有核验全部通过时，以下命令才清理与正式文件大小和 SHA-256 均相同的浏览器临时副本：

```powershell
python -u -m scripts.verify_downloads --cleanup-temp
```

正式文件和不匹配的临时文件会保留。核验依赖目录清单；仅使用单课程入口而未生成目录清单时，此工具不能核对完整目录。

## 8. 交互式资源探测

```powershell
python -u -m scripts.probe
```

默认打开 `COURSE_URL`，未填写时使用 `CATALOG_URL`；也可通过 `--url` 指定。工具启动独立 Edge，在该窗口登录并人工打开资源。

| 命令 | 功能 |
|---|---|
| `n` | 设置当前课程名 |
| `s` | 保存指定页面的 DOM 文字、链接和截图 |
| `l` | 列出已识别资源 |
| `d` | 保存已识别的完整 PDF / MP4 |
| `q` | 保存报告并退出 |

探测模式按资源哈希命名，主要用于适配和排查；它不遍历课程目录，去重仅限当前会话。HLS / DASH 播放清单会识别，但不会合并分段或标记成完整 MP4。

## 保存位置与恢复行为

默认保存到仓库根目录的 `output/`，不会因 Python 脚本移入 `scripts/` 而改变：

```text
output/
├── 示例课程-YY-MM/
│   ├── 01-章节中文名.pdf
│   └── 02-章节中文名.mp4
├── catalog-courses.json          # 目录顺序、页码、课程 ID 和类型
├── catalog-course-urls.txt       # 已确认的课程详情链接
├── official-downloads.json       # 成功 / 失败记录和文件大小
├── failed-downloads.json         # 最近一次失败清单
├── retry-pending-courses.json    # 未完成重试批次
├── download-verification.json   # 离线核验报告
└── last-download.json            # 最近一次官方下载诊断
```

文件名使用大纲中文标题和顺序编号，服务器生成的哈希不会拼进文件名。同名课程指向不同 ID、却将使用相同文件夹时，脚本停止该课程以避免覆盖。`FOLDER_SUFFIX` 默认使用运行当月；续传时保持这个值一致，已有成功记录指向的文件仍按原路径检查。

下载通过浏览器事件、完整 PDF 响应或官方接口返回的文件地址保存。浏览器空文件或超时会尝试本次事件给出的有效地址；视频使用官方事件地址流式保存完整 MP4。未完成文件保留为 `.part`，下次从头重试该文件，不支持按字节续传。课程中某章节失败会停止该课程的后续下载，再处理下一课程。

## 常见问题

| 情况 | 处理方法 |
|---|---|
| 提示网址未配置 | 复制并填写 `config.py`，或用命令行传入链接 |
| Chrome 连接超时 | 确认调试服务及端口，并处理浏览器中的“允许”提示 |
| 列表数量不一致 / 翻页后无变化 | 查看 `catalog-page.json`；检查页面和分页适配，避免降低数量要求来跳过遗漏 |
| 下载入口等待超时 | 增大 `--ready-timeout`；确认当前文件确实有官方下载入口 |
| 浏览器保存为空或停滞 | 使用当前版本的回退保存；查看 `last-download.json` 了解结果 |
| 运行锁提示已有脚本 | 等待现有进程退出，锁会自动释放；仅删除锁文件不能解决同时运行的问题 |
| 缺少 pypdf / Pillow / ffprobe | 安装核验依赖，并配置 ffprobe 的系统路径 |
| 页面结构不同 | 使用 `scripts/probe.py` 保存样本，再调整目录、章节及下载入口定位 |

## Windows 便捷入口

Windows 另提供统一的便捷入口；它创建仓库内 `.venv` 并安装对应依赖：

```powershell
.\scripts\run.ps1 catalog --yes
.\scripts\run.ps1 retry
.\scripts\run.ps1 verify
```

其他任务名为 `official` 和 `probe`。辅助脚本是可选方式，已配置 Conda 的用户可以直接使用前面的 Python 命令。

## 许可证

代码采用 [MIT 许可证](LICENSE)，许可证模板见 [Open Source Initiative](https://opensource.org/license/mit)。第三方依赖及下载的课程内容保留各自的权利和许可。
