# 终末地 Windows 音量联动：工具安装与使用手册

[← 返回 QA 案例首页](../README.md) · [详细采样与验证记录](evidence.md)

将 Windows 中 `Endfield.exe` 音频会话的音量和静音状态，单向同步到同一游戏启动的 Qt 网页音频服务。用于处理公告切换音效不随游戏主会话音量控制的问题。

独立 Python 程序，通过 Windows Core Audio 操作音频会话。游戏文件、内存、注册表和启动项均不修改；无需注入游戏。正常功能不需要管理员权限；无法核实的进程不接管。

## 控制范围

- 同步 Windows 游戏音量与静音两个独立状态，例如 0% 且未静音、20% 且静音。
- **不读取、不绑定游戏内“总音量”。** 游戏内为 0、Windows 游戏音量为 20% 且未静音时，网页音效仍可能播放。
- 不改变系统主音量，也不修改其他应用会话。
- 启动时读取当前状态并同步；新建网页会话会在后续扫描时加入。
- 游戏未启动或网页音频会话不存在时等待，不启动游戏、不自动打开公告。

## 首次安装

Windows 11 + Python 3.10 或更高版本；本机实测 Python 3.13.7。其他版本尚未逐一验证。

在本项目目录的 PowerShell 中运行：

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

依赖版本固定在 [requirements.txt](../requirements.txt)。创建环境和安装依赖后，日常使用无需重复此步。

## 使用

双击 `run.cmd`，或运行：

```powershell
& .\.venv\Scripts\python.exe audio_link.py
```

打开公告并切换一次栏目，以创建网页音频会话，然后在 **Windows 音量混合器中调整终末地**。程序会记录 `DISCOVER` 和 `SYNC` 事件。

启用联动后，网页会话以 Windows 游戏会话为准，可能解除之前单独设置的网页静音。

按 **Ctrl+C** 请求正常退出，程序尝试恢复各网页会话首次接管前的音量和静音，游戏主会话维持用户设置。仅对仍能核实身份、且没有在停用后发生独立修改的会话执行恢复。直接关闭控制台或强制结束进程不能保证执行退出恢复。

其他命令：

```powershell
# 只读查看当前游戏进程和相关音频会话
& .\.venv\Scripts\python.exe audio_link.py --status

# 只同步一次；退出后保留同步结果
& .\.venv\Scripts\python.exe audio_link.py --once

# 根据本机日志恢复仍存在的已接管会话
& .\.venv\Scripts\python.exe audio_link.py --restore

# 测试运行 15 秒，结束时恢复网页会话
& .\.venv\Scripts\python.exe audio_link.py --duration 15

# 可选：仅匹配指定游戏路径
& .\.venv\Scripts\python.exe audio_link.py --game-exe '<GameDir>\Endfield.exe'
```

使用 `--game-exe` 时，将 `<GameDir>` 替换为实际游戏安装目录。

`--leave-on-exit` 可让持续运行模式退出后保留同步状态，之后用 `--restore` 恢复。`--once` 在无可同步目标时返回退出码 2。所有命令也可以作为参数传给 `run.cmd`。

## 识别和同步规则

1. 校验 `Endfield.exe → PlatformProcess.exe → QtWebEngineProcess.exe` 父子关系，三个程序来自同一安装目录。
2. Qt 子进程必须带 `--type=utility` 和 `--utility-sub-type=audio.mojom.AudioService`；网页渲染进程不接管。
3. 不写死 PID。结合进程创建时间、设备和音频会话实例标识，避免进程重启、PID 复用后误认。
4. 优先选同游戏、同输出设备上的主游戏会话；跨设备时仅采用可唯一确定的主会话。多个候选中仅一个活跃时可选该活跃会话；仍有歧义则跳过。
5. 网页音量单向跟随主会话。运行期间手动修改网页音量，会在后续轮询中被重新同步。
6. 默认每 0.25 秒检查已发现会话的音量，每 2 秒重新扫描会话，约每 5 秒重新枚举输出设备。扫描本身占用时间，因此这些不是严格延迟上限；新设备、新会话可能有短暂延迟。
7. 网页需要静音时先静音再调音量；需要解除静音时先调整音量再解除，减少短暂音量突变。

## 日志和恢复

`.runtime/` 只存于本机，并被 `.gitignore` 排除：

- `audio-link.log`：轮转日志，单文件约 500 KB，最多保留两份旧日志。
- `restore.json`：写入前保存的接管原值与最近写入状态；原子替换保存。
- `evidence/`：此前排查的原始采样、截图和环境记录。
- `live-acceptance.json`：显式运行实机测试后的结果。

`--status` 输出和本机日志可能包含设备、进程或会话标识；分享前先做脱敏。同目录只允许一个联动／恢复实例。日志可用于恢复音频设置，不承担跨机器配置迁移。会话已经结束时，不把旧状态强加给新会话。异常终止后的部分写入、会话失效或外部修改可能使自动恢复跳过；查看日志后再处理，程序不会猜测覆盖。

## 测试

```powershell
# 逻辑和异常测试，不操作真实音量
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 显式实机验收：需保持公告打开；会短暂调整游戏音量与静音，然后恢复
& .\.venv\Scripts\python.exe tests/live_acceptance.py --confirm-audio-changes
```

[QA 案例](../README.md) · [实机结果与未验证场景](evidence.md)

## 当前限制

- 基于本机已观察的进程结构；游戏更新改变结构时会停止匹配，需要重新验证。
- 设备切换、重启和多设备场景的实测情况见测试记录，不以模拟测试代替。
- 同步依赖本程序保持运行；它不是游戏内总音量缺陷的内部修复。
- 不安装自启动、不提供图形界面；先保留为易读、可测试的独立脚本。

## 技术依据

- [微软：ISimpleAudioVolume](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nn-audioclient-isimpleaudiovolume)
- [微软：音频会话与进程标识](https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessioncontrol2-getprocessid)
- [pycaw 源码与文档](https://github.com/AndreMiras/pycaw)
