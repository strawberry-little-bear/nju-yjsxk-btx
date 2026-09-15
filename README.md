# NJU 研究生选课助手

一个基于 Selenium 的南京大学研究生选课页面自动化助手。它会打开 Chrome，自动填写账号密码，并按所选模式完成验证码登录：**无人值守模式**使用开源 ddddocr 自动识别验证码并自动提交登录（识别错误自动重试），**人工模式**由用户在浏览器中手动输入验证码并点击登录。登录成功后自动进入 `course_nju.html`，按照 `courses.json` 中的关键词持续检测课程，并在课程有余量时自动点击选课、处理确认弹窗。选课过程中若登录过期，会自动重新登录并恢复课程列表，全程可无人值守。

项目提供两种使用方式，任选其一即可：

- **命令行方式（原有方式，用法与参数完全保留）**：直接运行 `nju_yjsxk_btx.py`。适合习惯终端、需要挂到计划任务里、或者在服务器上运行的用户。
- **Web 控制台方式（新增，可选）**：运行 `web_dashboard/app.py`，在浏览器里看实时日志与课程状态，并启动/停止脚本、直接改账号和课程配置。适合不想一直开着终端盯日志的用户。

两种方式调用的是同一个主脚本，共用同一套 `config.json`、`courses.json`、`state.json` 和 `log/`，所以可以随时来回切换：命令行跑出来的进度，控制台里同样能看到；控制台改过的配置，命令行也会照常读取。

> 免责声明：本项目仅用于个人学习和自动化研究。请遵守南京大学选课系统的使用规定，请合理设置刷新间隔，不要高频刷新、恶意占用资源或进行任何未经授权的操作。选课结果以学校系统最终显示为准。

## 功能概览

| 功能 | 说明 |
| --- | --- |
| Chrome 自动启动 | 使用本机 Chrome 和 Selenium，每次运行使用独立的 Chrome 配置目录，不占用日常浏览器配置 |
| 自动填写账号密码 | 从本地 `config.json` 读取，不提交到仓库 |
| 两种验证码方式 | 加 `--use-login-helper`：ddddocr 自动识别（无人值守）；不加：人工输入 |
| 验证码识别重试 | 自动模式下识别错误/验证码错误会自动刷新重试，默认最多 6 次 |
| 弹窗自动关闭 | 自动关闭登录与选课过程中的弹窗，支持普通弹窗、iframe 内弹窗和 JS alert |
| 登录状态识别 | 检测登录框消失 + 首页特征（我的选课/退出登录/个人信息等），连续两轮稳定才判定成功 |
| 自动进入课程页 | 登录成功后自动跳转 `course_nju.html` |
| 会话过期自动恢复 | 选课过程中登录过期，自动重新登录并恢复课程列表，继续选课 |
| 关键词匹配 | 从 `courses.json` 读取启用课程关键词 |
| 状态检测 | 区分“找到但已满”和“没有找到” |
| 自动选课 | 课程有余量时点击选课按钮并处理确认弹窗 |
| 详细日志 | 打印匹配结果、按钮状态、弹窗内容和网站反馈 |
| 运行日志归档 | 每次运行自动保存带时间戳的日志到 `log/` |
| 任务状态持久化 | 每轮保存成功课程、待选目标、失败原因和刷新次数到 `state.json` |
| 安全测试模式 | `--dry-run` 只检测；`--test-click` 忽略“已满”文本，仅点击一次后退出 |
| Web 控制台（新增） | 浏览器里查看运行状态、课程进度和一键启动/停止脚本，命令行方式不受影响 |
| 实时日志推送（新增） | 通过 SSE 跟随最新日志文件自动滚动，断线自动重连并补拉最近日志 |
| 配置可视化编辑（新增） | 在控制台里直接维护账号与课程配置，不必手工编辑 JSON |
| 跨平台浏览器支持 | `browser_support.py` 依次尝试 `CHROMEDRIVER`、webdriver-manager 缓存、系统 PATH 与 Selenium Manager，Windows / macOS / Linux 通用 |

## 工作流程

### 无人值守模式（推荐，加 `--use-login-helper`）

```mermaid
flowchart TD
    A[启动脚本] --> B[打开登录页并自动填账号密码]
    B --> C[截图验证码 → ddddocr 识别]
    C --> D[填验证码 → 点登录/按回车]
    D --> E{识别或登录失败?}
    E -->|是 刷新验证码重试| C
    E -->|否| F{登录检测}
    F -->|登录框仍可见| E
    F -->|首页特征稳定两轮| G[进入 course_nju.html]
    G --> H[打开课程列表]
    H --> I[刷新并匹配关键词课程]
    I --> J{课程有余量?}
    J -->|否| I
    J -->|是| K[点击选课按钮]
    K --> L[处理确认弹窗并打印结果]
    L --> I
```

### 人工验证码模式（不加 `--use-login-helper`）

```mermaid
flowchart TD
    A[启动脚本] --> B[打开登录页并自动填账号密码]
    B --> C[用户手动输入验证码并点击登录]
    C --> D{登录检测}
    D -->|登录框仍可见| C
    D -->|首页特征稳定两轮| E[进入 course_nju.html]
    E --> F[按 courses.json 关键词刷新匹配]
    F --> G{课程有余量?}
    G -->|否| F
    G -->|是| H[点击选课并处理确认弹窗]
    H --> F
```

### 登录状态判断

脚本不会因为 URL 变成 `index_nju.html` 就立即跳转。登录前后状态大致如下：

```text
登录前：可见登录框=2，首页文字特征=未发现，首页结构=False
登录后：可见登录框=0，首页文字特征=我的选课，首页结构=True
```

只有登录框不可见，并且检测到“我的选课”“退出登录”等首页特征，连续两轮成立后，脚本才会进入课程页。

### Web 控制台方式（可选，新增）

控制台本身不参与选课逻辑，它相当于主脚本的“遥控器 + 仪表盘”：

```mermaid
flowchart TD
    A[python web_dashboard/app.py] --> B[浏览器打开 127.0.0.1:5000]
    B --> C[控制面板选择登录模式与刷新间隔]
    C --> D[点击启动]
    D --> E[控制台以子进程方式运行 nju_yjsxk_btx.py]
    E --> F[主脚本照常登录、刷新、匹配关键词、自动选课]
    F --> G[日志写入 log/，状态写入 state.json]
    G --> H[控制台读取并展示：状态卡片 / 课程状态 / 实时日志]
    H --> I{还需要继续跑吗?}
    I -->|继续观察| H
    I -->|点击停止| J[终止主脚本及其 Chrome 子进程]
```

命令行方式依然是完整可用的，控制台只是多了一种入口，不启动它也没有任何影响。

## 环境要求

- Windows 10/11 或 macOS 12 及以上版本（代码层面同时兼容 Linux，未做完整验证）
- Python 3.10 或更高版本
- 已安装 Google Chrome
- 能够访问南京大学研究生选课系统

项目会优先使用环境变量 `CHROMEDRIVER` 指定的 Driver，其次按顺序查找 webdriver-manager 缓存与系统 PATH，最后交给 Selenium Manager 处理，因此 Windows 和 macOS 都不需要写死 Driver 路径。若 Chrome 不在默认安装位置，可通过环境变量 `GOOGLE_CHROME_BIN` 指定 Chrome 可执行文件路径。这部分逻辑集中在 `browser_support.py`，详见“跨平台浏览器与 Driver”。

只在需要 Web 控制台时才会用到 `Flask`，纯命令行使用可以忽略它。

安装 Python 依赖：

Windows PowerShell：

```powershell
python -m pip install -r requirements.txt
```

macOS Terminal：

```bash
python3 -m pip install -r requirements.txt
```

依赖包括：`selenium`（浏览器自动化）、`ddddocr`（离线验证码识别）、`Flask`（Web 控制台，仅在使用控制台时需要）、`requests`（可选，sai 实验功能使用）。

## 配置

### 1. 创建本地账号配置

复制 `config.example.json` 为 `config.json`，再填写个人账号密码：

```json
{
  "UserId": "你的学号",
  "PassWd": "你的密码",
  "url": "https://yjsxk.nju.edu.cn"
}
```

`config.json` 已加入 `.gitignore`，不会被提交到仓库。请不要把账号密码粘贴到 README、Issue 或截图中。

### 2. 配置目标课程

编辑 `courses.json`：

```json
{
  "courses": [
    {
      "name": "高级编程语言设计与实现",
      "keywords": ["高级编程语言"],
      "enabled": true
    }
  ]
}
```

匹配规则是：课程行文本中包含任意一个启用关键词即可。建议使用稳定、独特的关键词，避免匹配到不想选的同名课程。

这两个文件都可以用任意编辑器手工修改（命令行方式只需要这样），也可以在 Web 控制台里可视化编辑后保存，两种方式改的是同一份文件。控制台保存时会做基本校验（例如系统地址必须是合法的 http/https 链接），并且用“临时文件 + 原子替换”的方式写入，避免中途失败把配置写坏。

## 使用方式

驱动选课逻辑的方式有两种，按自己的习惯选一个即可：

- **命令行**：直接运行 `nju_yjsxk_btx.py`，功能最完整，所有参数都能用。
- **Web 控制台**：运行 `web_dashboard/app.py`，在浏览器里操作，详见下面的「Web 控制台（可选，新增）」一节。

先讲命令行方式。Windows 使用 `python`，macOS 使用 `python3`。完整参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--courses` | `courses.json` | 课程关键词配置文件路径 |
| `--min-interval` | `60` | 最短刷新间隔（秒） |
| `--max-interval` | `300` | 最长刷新间隔（秒） |
| `--timeout` | `30` | 页面元素等待超时（秒） |
| `--dry-run` | 关闭 | 只检测课程，不点击选课 |
| `--test-click` | 关闭 | 忽略“已满”文字，仅点击第一条匹配课程一次后退出 |
| `--missing-rounds` | `5` | 连续多少轮找不到目标关键词后退出 |
| `--use-login-helper` | 关闭 | 使用 login_helper + ddddocr 全自动登录（无人值守） |
| `--login-max-attempts` | `6` | 自动登录最大尝试次数 |

### 无人值守模式（推荐）

自动识别验证码、自动登录、自动选课，全程无需人工：

```powershell
python nju_yjsxk_btx.py --use-login-helper --courses courses.json
```

macOS：

```bash
python3 nju_yjsxk_btx.py --use-login-helper --courses courses.json
```

运行过程：

1. Chrome 打开登录页，自动填入账号密码。
2. 自动截图验证码，`captcha_ocr.py`（ddddocr）离线识别，结果强制大写。
3. 自动填入验证码并提交：优先点击登录按钮，找不到按钮时自动在输入框按回车提交。
4. 若是验证码识别错误或提示“验证码不正确”，自动点击验证码刷新并重试，最多 `--login-max-attempts` 次（默认 6 次）。
5. 登录成功后自动进入 `course_nju.html`。
6. 按随机间隔刷新并检测课程，有余量自动点击选课、处理确认弹窗。
7. 选课过程中登录过期，自动重新登录并恢复课程列表，继续选课。

### 人工验证码模式

只自动填账号密码，验证码由人工输入并手动点击登录，其余流程自动：

```powershell
python nju_yjsxk_btx.py --courses courses.json
```

macOS：

```bash
python3 nju_yjsxk_btx.py --courses courses.json
```

运行后：

1. Chrome 自动打开登录页并填写账号密码。
2. 用户在浏览器中手动输入验证码并点击登录。
3. 终端确认登录框消失、首页特征出现后，脚本进入 `course_nju.html`。
4. 脚本按随机间隔刷新并检测课程，有余量自动选课。

### 安全检测模式

只检测课程，不点击选课按钮：

```powershell
python nju_yjsxk_btx.py --dry-run --use-login-helper
```

macOS：

```bash
python3 nju_yjsxk_btx.py --dry-run --use-login-helper
```

### 单次点击测试

如果课程当前显示“已满”，但需要验证按钮定位和点击流程，可以使用：

```powershell
python nju_yjsxk_btx.py --test-click --use-login-helper
```

该模式会忽略课程行里的“已满”文字，对第一条关键词匹配课程执行一次实际点击，打印按钮属性和弹窗反馈，然后退出。它适合排查自动化流程，不代表学校系统会接受选课。

## Web 控制台（可选，新增）

控制台是新增的可选入口：不启动它，命令行方式完全照旧。它位于 `web_dashboard/`，用 Flask 提供一个单页界面。

### 启动与参数

确认依赖已安装（`requirements.txt` 已包含 Flask），然后在项目根目录启动。

Windows PowerShell：

```powershell
python web_dashboard\app.py
```

macOS / Linux：

```bash
python3 web_dashboard/app.py
```

然后浏览器访问 <http://127.0.0.1:5000>。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 监听地址，默认只有本机可以访问 |
| `--port` | `5000` | 监听端口，被占用时可以换成 5001 等 |

例如换端口启动：

```powershell
python web_dashboard\app.py --port 5001
```

### 界面上能做什么

- **控制面板**：选择登录模式（人工验证码 / OCR 无人值守）、切换安全检测模式、设置最小与最大刷新间隔，然后一键启动或停止主脚本。
- **状态卡片**：累计刷新次数、已选成功数、待选目标数、上次运行时间。
- **课程状态**：逐个列出启用课程的当前状态（已选成功 / 待选 / 未找到）及其关键词。
- **实时日志**：跟随最新日志文件自动滚动，断线后会重连并补拉最近 100 行。
- **账号配置**：修改学号、系统地址与密码；密码框留空表示不修改，页面不会回显密码明文。
- **课程管理**：增删课程、修改名称与关键词、启用或停用课程，保存后写回 `courses.json`。

### 和命令行方式的关系

- 控制台启动的就是同一个 `nju_yjsxk_btx.py`，工作目录、配置文件、日志目录与状态文件完全一致，两种方式可以随时混用。
- 控制台启动时拼接的参数是 `--courses`、`--min-interval`、`--max-interval`、`--login-max-attempts`（固定 6 次），并按界面选择追加 `--dry-run` 或 `--use-login-helper`。
- 界面只覆盖常用参数，是命令行参数的子集。需要 `--timeout`、`--missing-rounds`、`--test-click` 这类选项时，请直接用命令行运行。
- 主脚本的进程信息会写入 `.dashboard-process.json`（已加入 `.gitignore`）。控制台重启后仍能识别由它启动、且还在运行的主脚本；点“停止”会终止该主脚本及其 Chrome 子进程。
- 关闭控制台（终端 `Ctrl+C`）时会一并停止它启动的主脚本。如果希望脚本长期挂机、不受终端影响，用命令行方式运行更合适（可配合 `nohup`、`screen` 或系统计划任务）。

### 安全提示

- 控制台默认只监听 `127.0.0.1`，仅本机可访问。**不建议**改成 `--host 0.0.0.0`：控制令牌会随首页一起返回给访问者，暴露到局域网后，同网段的其他设备同样能启动或停止你的选课脚本，并读取课程配置。
- 启动、停止、保存配置这些写操作都要求请求头里带正确的控制令牌。令牌默认在每次启动时随机生成，只嵌入本机页面；如果你要写脚本调用接口，可以用环境变量 `NJU_DASHBOARD_TOKEN` 固定它。
- 控制台保存账号配置时，密码与命令行方式一样以明文存放在 `config.json` 中（页面本身不会回显密码），请勿提交或分享该文件。
- 页面样式通过 CDN 加载 Tailwind CSS。没有外网时界面会退化成无样式的裸页面，但功能不受影响。

### 接口一览

如果打算自己写脚本对接控制台，可用的接口如下（写操作需要带 `X-Dashboard-Token` 请求头）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 运行状态、刷新次数、上次运行时间、课程状态、登录状态 |
| GET | `/api/process` | 主脚本进程信息（PID 与命令行） |
| GET | `/api/logs?tail=200` | 读取最新日志末尾若干行（1–1000） |
| GET | `/api/logs/stream` | SSE 实时日志流 |
| POST | `/api/start` | 启动主脚本 |
| POST | `/api/stop` | 停止主脚本 |
| GET / POST | `/api/config/account` | 读取 / 保存账号配置 |
| GET / POST | `/api/config/courses` | 读取 / 保存课程配置 |

## 验证码识别模块（captcha_ocr.py）

- 基于开源 [ddddocr](https://github.com/sml2h3/ddddocr)，纯离线本地识别，不需要联网。
- 识别结果强制大写，只保留字母和数字。
- 默认读取 `captcha_screenshots/` 目录里最新的一张图片，也可指定图片路径。

单独测试验证码识别：

```powershell
python captcha_ocr.py                        # 识别 captcha_screenshots 最新图
python captcha_ocr.py 某图片.png             # 识别指定图片
```

代码内调用：

```python
from captcha_ocr import recognize_image
code = recognize_image("captcha_screenshots/1.png")
```

## 登录实现说明（login_helper.py）

主脚本在加了 `--use-login-helper` 时，通过 `login()` 调用 `login_helper.perform_login()` 完成登录，主要能力：

- **自动识别**：截图验证码 → `captcha_ocr` 识别 → 填入 → 提交（优先点登录按钮，找不到按钮自动按回车）。
- **失败重试**：验证码识别为空或页面提示“验证码不正确”“密码错误”等，自动刷新验证码重试，最多 `--login-max-attempts` 次。
- **弹窗自动关闭**：登录过程中出现的普通弹窗、iframe 内弹窗、JS alert 都会自动点击关闭，避免遮挡登录。
- **登录成功判定**：登录框消失且首页特征连续两轮稳定，才认为登录成功。

`login_helper.py` 也可以单独运行：

```powershell
python login_helper.py                        # 自动 OCR 登录
python login_helper.py --manual               # 人工输入验证码
python login_helper.py --capture-only         # 只填账号并截图验证码
python login_helper.py --max-attempts 8       # 自定义重试次数
```

## 会话过期自动恢复

选课过程中如果登录过期（页面出现“未登录不能选课”“登录超时”，或重新出现登录框），主脚本会自动执行：

1. 先把当前任务进度写入 `state.json`。
2. 自动跳回登录页，通过 login_helper 自动重新登录（OCR 识别验证码，失败自动重试）。
3. 登录成功后自动回到 `course_nju.html`，重新定位方案课程列表。
4. 继续原来的选课循环，已选成功的课程和剩余待选目标都不丢失。

整个过程无需人工干预，可长时间挂机。

## 跨平台浏览器与 Driver（browser_support.py）

新增的 `browser_support.py` 统一负责 Chrome 与 ChromeDriver 的定位和启动，主脚本和 `login_helper.py` 都通过它创建浏览器，因此 Windows 与 macOS 不需要改代码：

1. **Driver 查找顺序**：环境变量 `CHROMEDRIVER` → webdriver-manager 缓存（`~/.wdm/drivers/chromedriver`）→ 系统 `PATH` → 交给 Selenium Manager 自动解析。
2. **Chrome 查找顺序**：环境变量 `GOOGLE_CHROME_BIN` → Windows 的 `Program Files`、`Program Files (x86)`、`LOCALAPPDATA`，macOS 的 `/Applications` 与 `~/Applications`，Linux 的 `/usr/bin/google-chrome`、`chromium`、`chromium-browser`。
3. **独立配置目录**：每次运行都会在项目目录下创建 `.chrome-profile-<时间戳>`，不占用你日常使用的 Chrome 配置；该目录已被 `.gitignore` 忽略，可以随时删除。
4. **版本兜底**：本地缓存的 Driver 若落后于已自动升级的 Chrome，会先尝试缓存 Driver，失败后自动回退到 Selenium Manager 解析，避免直接报“版本不匹配”。

## 日志示例

### 找到课程但课程已满

```text
[刷新] 第 11 次刷新开始；距离上一轮刷新 0.0 秒。
[课程匹配] 本轮找到 8 条待选关键词课程；剩余目标：高级编程语言, 软件安全, ...
[课程匹配 1] 已找到 [已满/不可选]：085212D28-高级编程语言设计与实现 ... 已满 选课
[选课动作 1] 跳过点击：课程已满或不可选。
[选课结果] 找到了关键词课程，但当前没有可选课程，继续刷新。
[等待] 下一轮刷新将在 166.9 秒后开始。
```

### 自动登录与验证码重试

```text
[登录尝试] 第 1/6 次
[OCR] 识别结果：VAN
[登录检测] 页面提示：验证码不正确
[登录尝试] 本次未成功（验证码不正确），刷新验证码重试。
[登录尝试] 第 2/6 次
[OCR] 识别结果：F4P6
登录成功。当前URL=https://yjsxk.nju.edu.cn/yjsxkapp/sys/xsxkapp/index_nju.html
已进入课程页面，开始检测课程。
```

### 选课成功

```text
[选课动作] 找到有余量课程，准备点击选课按钮：...
[确认弹窗] 点击前内容：确认选择该课程？
[选课成功] 已完成目标：高级编程语言；剩余目标：软件安全, ...
```

### 会话过期自动恢复

```text
[会话] 检测到登录已失效，已保存 state.json。
[会话] 正在重新打开登录页；使用 login_helper 自动重新登录（OCR 识别验证码）。
[登录尝试] 第 1/6 次
[OCR] 识别结果：Q9YS
[登录尝试] 验证码：Q9YS
已按回车提交登录；等待登录结果。
登录成功。当前URL=.../index_nju.html
[会话] 重新登录成功，已恢复课程列表和待选目标。
```

## 常见问题

### 为什么进入 `index_nju.html` 后没有立即跳转？

这是设计行为。这个站点的登录页和登录后的首页可能使用同一个 URL，因此脚本还会检查：

- 登录框是否仍然可见；
- 是否出现“我的选课”“退出”等登录后特征；
- 登录成功状态是否连续稳定两轮。

### 为什么显示“找到课程”但没有点击？

如果课程行包含“已满”“满额”“无余量”或“不可选”，脚本会打印“跳过点击”，这是正常保护逻辑。可以用 `--test-click` 单独验证按钮点击流程。

### 为什么点击后显示失败？

按钮点击成功只代表浏览器触发了页面操作，最终结果由学校系统业务规则决定。脚本会打印确认弹窗、提示框和失败分类，常见原因包括课程已满、时间冲突、先修条件不满足、重复选课等。失败后目标课程默认保留，下一轮继续尝试。

### 验证码识别错了怎么办？

自动模式下脚本会检测到“验证码不正确”等提示，自动刷新验证码重试，默认最多 6 次（可用 `--login-max-attempts` 调整）。个别情况下 ddddocr 识别率偏低，可以多试几次或改用人工验证码模式：`python nju_yjsxk_btx.py`（不加 `--use-login-helper`）。

### 登录时弹出弹窗挡住登录怎么办？

脚本会自动检测并关闭登录过程中的弹窗，包括普通弹窗、iframe 内弹窗和 JS alert，然后自动重新点击登录或按回车提交。

### 选课过程中登录过期了怎么办？

脚本会自动检测登录失效并重新登录，然后回到课程页继续选课，无需人工处理，详见“会话过期自动恢复”。

### Chrome 无法启动怎么办？

请先关闭正在运行的自动化 Chrome 窗口，再重试。脚本会为每次运行创建独立的 Chrome 配置目录，避免占用日常 Chrome 配置。若仍失败，请确认 Chrome 已正确安装，并检查 Python 与 ChromeDriver 版本是否匹配。

### 如何停止脚本？

在运行脚本的终端按 `Ctrl+C`。如果浏览器仍保持打开，按提示回车即可关闭。

### 可以只用命令行、不用 Web 控制台吗？

可以。控制台是新增的可选组件，不启动它，命令行方式的行为和参数与以前完全一致，只是安装依赖时多了一个 Flask。两种方式共用同一份配置和日志，随时可以切换。

### Web 控制台打不开或提示端口被占用怎么办？

先确认是在项目根目录执行，并已安装依赖（`python -m pip install -r requirements.txt`）。如果 5000 端口被占用，换一个端口启动：`python web_dashboard\app.py --port 5001`，然后访问 <http://127.0.0.1:5001>。

### 控制台里的实时日志一直是空的？

控制台跟随的是 `log/` 目录下最新的 `run_*.log`。如果还没运行过脚本，该目录是空的，日志区自然没有内容；先在控制台点一次“启动”，或直接用命令行跑一次即可。

### 控制台的“停止”和直接按 Ctrl+C 有什么区别？

控制台里的“停止”会终止它启动的主脚本及其 Chrome 子进程；在终端按 `Ctrl+C` 关闭控制台时，也会尝试一并停止这些子进程。想让脚本脱离终端长期运行，请用命令行方式启动（例如配合 `nohup`、`screen` 或系统计划任务）。

## 项目文件

```text
nju-yjsxk-btx/
├─ nju_yjsxk_btx.py       # 主脚本：登录（可选 OCR）、课程匹配、检测和选课
├─ login_helper.py        # 登录辅助模块：截图/OCR 接入/失败重试/弹窗关闭/回车兜底
├─ captcha_ocr.py         # 验证码识别模块（ddddocr），可独立运行或代码调用
├─ browser_support.py     # 跨平台 Chrome/ChromeDriver 定位与独立配置目录启动
├─ web_dashboard/         # Web 控制台（Flask，可选）
│  ├─ app.py              # 控制台后端：状态与日志接口、启动/停止主脚本、配置读写
│  └─ templates/
│     └─ dashboard.html   # 控制台单页界面
├─ courses.json           # 课程关键词配置
├─ config.example.json    # 配置模板，不含个人信息
├─ config.json            # 本地真实配置，不提交到 Git
├─ state.json             # 运行状态（自动生成，不提交到 Git）
├─ log/                   # 每次运行的日志归档（自动生成，不提交到 Git）
├─ requirements.txt       # Python 依赖（selenium / ddddocr / Flask / requests）
├─ README.md              # 项目说明
└─ .gitignore             # 敏感配置与临时文件忽略规则
```

## 版本管理说明

本项目使用 Git 管理。提交前请确认：

```powershell
git status
git diff --check
```

并确认输出中没有 `config.json`、密码、验证码、Chrome 配置目录（`.chrome-profile-*`）或控制台进程标记（`.dashboard-process.json`）。

## 许可证

本项目采用 [MIT License](https://github.com/strawberry-little-bear/nju-yjsxk-btx/blob/main/LICENSE)。许可证全文请参阅仓库根目录的 `LICENSE` 文件。
