# 在另一台电脑安装「读取微信群聊天」

**第一次可以直接将 ZIP 交给新电脑上的 Codex**，告诉它「请安装这个本地 Skill，按包内的中文说明检查运行条件」。如果只需要分析原电脑已经导出的聊天，把该群的分析文本单独交给 Codex 即可。需要查看新消息时，使用新电脑上已同步的记录。

## 1. 导入 Skill

Mac 上把 ZIP 放在「下载」，打开终端执行以下命令。目标目录已存在时，先核对版本再更新。

```sh
SKILL_ZIP="$HOME/Downloads/wechat-chat-reader.zip"
SKILLS_ROOT="$HOME/.agents/skills"
SKILL_DIR="$SKILLS_ROOT/wechat-chat-reader"
mkdir -p "$SKILLS_ROOT"
test ! -e "$SKILL_DIR" && ditto -x -k "$SKILL_ZIP" "$SKILLS_ROOT"
test -f "$SKILL_DIR/SKILL.md"
```

打开新 Codex 会话，输入 `$wechat-chat-reader`。如果列表尚未刷新，重启 Codex。Windows 也可将 ZIP 中的整个 `wechat-chat-reader` 文件夹解压到用户目录的 `.agents/skills` 下，用已有导出或可用的微信窗口分析聊天。旧版 Codex 若使用 `~/.codex/skills`，可将 Skill 放到该目录，保留一份即可。[Codex Skills 文档](https://developers.openai.com/codex/skills/)

## 2. 选择新电脑上的数据来源

| 你已有的材料 | 接下来怎么做 |
| --- | --- |
| 已导出的群聊文本、JSON 或本工具生成的导出目录 | 指定文件给 Codex。可跨平台使用，跳过后面的 CLI 安装。 |
| 已登录的微信，可以打开该群 | 使用 Codex 当前可用的电脑操作工具，在微信聊天记录窗口按日期查看。所需历史没出现在电脑上时，先通过微信的「迁移与备份」将指定聊天迁移或恢复到登录的设备，再在群里核对日期。 |
| 已有本机微信数据库和匹配它的密钥文件，或兼容的明文数据库目录 | 在 Apple 芯片、macOS 14 及以上的电脑按第 3–5 步使用数据库模式。Intel Mac、Windows 及 Linux 的本地数据库读取未随此包提供。 |

### 新机没有数据库密钥

旧机当时通过重签微信并从运行进程中取得凭据，随后用已有密钥读取数据库。直接查询新机的加密库仍需要与该库匹配的密钥。只需在新机查看或分析群聊时，可以在导入 Skill 后直接选下面一种方式。

**想看新机上的最新讨论**。在新机登录微信，打开目标群，确认能看到消息。如果缺少需要的历史，在微信自带的迁移或恢复功能中选择该群，完成后在新机核对起止日期。给具备电脑操作能力的 Codex 发这句话即可：

> 使用 $wechat-chat-reader，通过已登录的微信窗口查看【群名】最近 7 天的聊天。请先告诉我实际能看到的最早和最新消息时间，再整理讨论内容。

**想在新机分析一段完整的旧记录**。在已经可以读库的旧机上，给 Codex 发下面这句话：

> 使用 $wechat-chat-reader，完整导出【群名】从【开始日期】到【结束日期】的本地聊天。请核对分页是否结束，按需补全引用，并告诉我可复制到新机的文件路径。

把这次导出的 `transcript.txt` 和 `summary.json` 复制到新机。若已补全引用，改用 `transcript-complete.txt`，并带上 `recovery-summary.json` 和原来的 `summary.json`。在新机让 Codex 分析这些文件即可。摘要可用来核对条数和覆盖日期，聊天正文以导出时为准。

## 3. 安装 Mac 数据库查询程序和 Python

此前试过的 WeFlow 和 GreenBubbles 图形界面均为可选软件。本节使用包内的命令行程序。

先运行 `uname -m` 和 `sw_vers -productVersion`，确认是 `arm64` 和 macOS 14 或以上。在导入 Skill 的同一终端运行：

```sh
sh "$SKILL_DIR/scripts/install_cli.sh"
export PATH="$HOME/.local/bin:$PATH"
greenbubbles --version
python3 --version
```

安装脚本从包内离线安装 `greenbubbles 0.10.0` 到 `~/.local/bin`，校验固定 SHA-256 和开发者签名（Team ID `5BR9M56H9W`）。已有其他版本时，用 `--prefix '/另一个绝对路径'` 另行安装，并将对应的 `bin` 目录放到 PATH 最前。许可在 `assets/greenbubbles/`。[上游发行版](https://github.com/bojieli/greenbubbles/releases/tag/v0.10.0) · [上游校验和](https://github.com/bojieli/greenbubbles/releases/download/v0.10.0/SHA256SUMS-0.10.0.txt)

普通读取使用 Python 3.9 或更高版本及其标准库。已有可用 Python 时可直接进入第 4 步。新装推荐 Python 3.12，可选适配本机系统的官方 macOS 安装器。已有 Homebrew 且支持当前系统时，也可执行以下命令。[Python 官方 macOS 安装包](https://www.python.org/downloads/macos/) · [Homebrew Python 3.12](https://formulae.brew.sh/formula/python@3.12)

```sh
brew install python@3.12
PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
"$PYTHON_BIN" --version
```

## 4. 验证数据库、权限和最近的消息

选择目标账号的 `db_storage` 目录，里面通常有 `contact/`、`session/`、`message/`。Mac 微信的常见位置是 `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号目录>/db_storage`。以本机实际保存位置为准。微信的备份文件需先用微信恢复，在目标群中确认所需时间的聊天可见。

在终端设置这台电脑的实际路径及群名。这里传入的是凭据文件的路径。凭据文件须归当前用户所有，且只允许本人读取（如 `0600`）。

```sh
SOURCE='/目标账号目录/db_storage'
KEY_FILE='/私有目录/已有的匹配密钥文件'
GROUP='你的微信群全名'
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" "$SKILL_DIR/scripts/read_chat.py" \
  --source "$SOURCE" --key-file "$KEY_FILE" --conversation "$GROUP" --limit 5
```

兼容明文数据库的调用方式为：

```sh
"$PYTHON_BIN" "$SKILL_DIR/scripts/read_chat.py" \
  --source "$SOURCE" --decrypted --conversation "$GROUP" --limit 5
```

如果 macOS 询问能否访问其他 App 的数据，在弹窗中允许后重试。打开返回路径里的 `summary.json`，核对消息数、最新消息时间和警告。新会话如需记住路径，可在 `~/.config/wechat-chat-reader/source.json` 保存 `{"source":"/目标目录/db_storage","key_file":"/私有凭据文件路径"}`。使用明文源时写 `{"source":"/明文目录/db_storage","decrypted":true}`。Codex 可按这份本机配置组装参数。原始密钥和聊天内容留在本机。

## 5. 需要补全引用回复时

如果要分析的消息包含空的引用回复，使用带 `sqlite3.Connection.deserialize` 的 Python 3.12，另装 `cryptography` 和 zstd 动态库。下例使用 Homebrew。请将 `PYTHON_BIN` 设为实际 Python 3.12 的路径。

```sh
brew install zstd
READER_VENV="$HOME/.local/share/wechat-chat-reader/venv"
"$PYTHON_BIN" -m venv "$READER_VENV"
"$READER_VENV/bin/python" -m pip install cryptography
"$READER_VENV/bin/python" -c 'import cryptography, sqlite3; assert hasattr(sqlite3.Connection, "deserialize")'
```

使用这个虚拟环境里的 Python 执行恢复命令。已导出的分页与恢复后的文本均可在本地重新核对。[引用回复说明](quotes.md)
