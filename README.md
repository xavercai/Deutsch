# 德语跟读课堂 · 在线版（GitHub Pages）

手机上独立运行、每天自动更新素材与语音的版本。**不需要电脑，也不需要任何 API Key。**

## 这套东西怎么运作

| 文件 | 作用 |
| --- | --- |
| `index.html` | 工具本体（单文件） |
| `news/daily.json` | 当天素材，页面打开时读它（同源，无跨域问题） |
| `news/audio.json` | 文本 → 音频文件 的清单，页面据此播放 Conrad 德语语音 |
| `news/audio/*.mp3` | 预合成好的 Conrad 德语音频（由工作流生成，不在仓库初次上传内容里） |
| `scripts/fetch_daily.py` | 抓德语新闻、挑 3–5 句、写 `news/daily.json`（纯标准库） |
| `scripts/build_audio.py` | 用免 Key 的 `edge-tts` 把「内置语料 + 当天新闻 + 本周新语料」合成为 MP3；并按**音频里的真实停顿**切词块，写 `news/audio.json` + `news/chunks.json` |
| `scripts/translate_zh.py` | 可选：用 AI 接口给周更/新闻句补中文翻译（未配 Key 则跳过） |
| `scripts/corpus.json` | 内置语料里全部可播放文本（520 条：整句 + 词块），供上一步合成 |
| `.github/workflows/daily.yml` | 每天北京时间 05:00 自动跑上面两步并提交 |

**为什么不需要 Key**：电脑版用的是 `edge-tts`（微软 Edge「大声朗读」同源神经语音），它本身免 Key。
现在把这一步搬到 GitHub 的服务器上跑，合成结果提交进仓库，网页直接播放这些 MP3。
所以手机上听到的是**和电脑上完全一样的 Conrad 德语男声**，且不用注册任何账号。

页面加载顺序：读 `news/audio.json`（拿到 Conrad 语音）→ 读 `news/daily.json`（拿到当天素材）→
任一缺失就退回浏览器自己抓 RSS / 内置 105 句语料。任何一步失败都不会白屏。

## 一次性部署（约 10 分钟）

1. **建仓库**：github.com → `New repository` → 名字随意 → **Public** → Create。
   > 免费账号的 Pages 只支持公开仓库。本包不含任何密钥或个人信息。

2. **上传文件**：仓库页 → `Add file` → `Upload files` → 把本文件夹里**全部内容**拖进去
   （`index.html`、`.nojekyll`、`README.md`，以及 `news/`、`scripts/`、`.github/` 三个文件夹）→ Commit。
   > `.github` 是点开头的文件夹，网页上传可能拖不进去；见下面「常见问题」。

3. **开 Pages**：仓库 `Settings` → `Pages` → Source = `Deploy from a branch` → Branch `main`、`/ (root)` → Save。
   > 不要选 `GitHub Actions` 作为 Source。

4. **跑一次工作流**：仓库顶部 **`Actions`** 标签 → 左侧选 `daily-news` → `Run workflow`。
   第一次会合成约 520 条内置语料音频，需要几分钟；跑完会提交 `news/audio/`（约 15–20 MB）和 `news/audio.json`。
   之后每天 05:00 自动跑，只补当天新闻的那几条。

5. 访问 `https://<用户名>.github.io/<仓库名>/`，手机上 Safari 打开 → 「分享 → 添加到主屏幕」。

## 手机上不需要配置任何东西

页面会自动检测到 `news/audio.json`，把语音引擎切到 **📦 离线语音包**，直接用 Conrad 音色播放。

> 如果你之前在手机上手动点过别的引擎（比如「☁️ 云 TTS」），它不会自动切回来。
> 到「⑥ 设置 → 语音引擎」点一下 **📦 离线语音包** 即可；或清除该网站数据后重开。

## 可选：让机器挑的句子也有中文翻译

内置精编语料自带人工中文；周更/新闻句是机器从正文挑的，默认没有整句翻译（卡片会自动改用逐词中文注释）。
想让它们也有和内置语料一样的中文，加一个 Secret 就行：

仓库 `Settings` → `Secrets and variables` → `Actions` → `New repository secret`：

| Name | 必填 | 说明 |
| --- | --- | --- |
| `AI_KEY` | 是 | 接口密钥 |
| `AI_ENDPOINT` | 否 | 默认 `https://api.openai.com/v1/chat/completions` |
| `AI_MODEL` | 否 | 默认 `gpt-4o-mini` |

只要接口是 **OpenAI 兼容的 `/chat/completions`** 都能用（OpenAI、DeepSeek、通义、Azure OpenAI、本地 vLLM/Ollama 兼容层…）。
没配 `AI_KEY` 时这一步直接跳过，不会报错、不影响其它功能。

## 注意

- `edge-tts` 不是微软对外承诺的正式 API，云端机房 IP 偶尔会被临时拒绝。真被拒时工作流会报错并保留旧数据，站点不会变空。
- 新闻音频保留 45 天（`build_audio.py` 里的 `NEWS_KEEP_DAYS`），语料音频永久保留。
- Action 抓不到任何新闻源时**不会覆盖**旧文件。
- 想改抓取时间：改 `.github/workflows/daily.yml` 的 `cron`（UTC）。想增删新闻源：改 `scripts/fetch_daily.py` 的 `FEEDS`。
- 想换音色：改 `scripts/build_audio.py` 顶部的 `VOICE`（必须是 edge-tts 支持的德语语音名，如 `de-DE-KillianNeural`）。

## 常见问题

**Q：`.github` 文件夹拖不进去怎么办？**
用 `Add file` → `Create new file`，文件名栏直接输入 `.github/workflows/daily.yml`（斜杠会自动建目录），粘贴内容后 Commit。

**Q：Actions 里找不到 `daily-news`？**
它在仓库顶部的 **`Actions`** 标签里，不在 `Settings` 里。直接打开 `https://github.com/<用户名>/<仓库名>/actions`。

**Q：工作流报 `403` 或合成全失败？**
微软临时拒绝了机房 IP。过一两天重跑；仍然不行就改用 Azure 免费 Key（工具里「☁️ 云 TTS」那一档）。

**Q：手机上还是机器人的声音？**
说明它没用上离线语音包。确认 `news/audio.json` 已经生成（仓库里能看到），再到设置里点「📦 离线语音包」。
