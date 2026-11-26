# 核对引用回复

在读取结果中查看 `summary.json` 的 `emptyQuotes`，以及 `transcript.txt` 中的引用正文占位。GreenBubbles 0.10.0 偶尔能识别消息类型为 Quote，却没有读出回复文字。此时可从同一数据源按原消息标识补读。恢复依赖的安装方式见 [中文安装说明](install-zh.md)。

把 `RUN` 设为刚才命令返回的 `outputDirectory`，`PY` 为装有可选依赖的 Python 可执行文件，`KEY_FILE` 为匹配该数据库的现有私有密钥文件路径。

```bash
"$PY" "$SKILL_DIR/scripts/recover_quotes.py" "$RUN" --key-file "$KEY_FILE"
```

已有兼容明文库时，用 `--decrypted` 替换 `--key-file "$KEY_FILE"`。较早的导出若未在摘要中记录源目录，可额外传入 `--source "$SOURCE"`。成功后在本次导出目录中使用 `transcript-complete.txt` 和 `recovery-summary.json`，检查剩余空引用数及身份核对结果。分页样本在合并后仍保持原来的时间和条数范围。

对于争议或人物观点，核对回复内容和被引用的发言。若对应分片缺失、消息已变化或补读后只剩附件占位，保留该处的缺口。原始分页和引用中的 XML 可能带有个人信息，整理时只选用任务需要的字段。
