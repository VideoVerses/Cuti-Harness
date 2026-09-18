# Cuti Harness

[English](README.md) | 中文

![Cuti Harness](docs/assets/cuti-harness-hero.jpg)

[![Webpage](https://img.shields.io/badge/Webpage-111827?style=for-the-badge&labelColor=38BDF8)](https://newai.land/cuti-harness) [![MIT License](https://img.shields.io/badge/MIT%20License-8B7CF7?style=for-the-badge)](LICENSE)

Cuti Harness 是一个基于 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的通用、开源、插件化视频 harness，面向长程视频创作。该 harness 负责对话、agent loop（智能体循环）和高层工具选择。视频 Runtime 负责持久化项目、Skill（技能）、产物依赖、增量构建、时间线、校验和导出。

本仓库包含 Cuti Harness 的 Agent 算法与循环、Video Runtime 后端、Video Studio 前端、兼容 BFF、Media Service、Sandbox Worker，以及它们使用的 Provider、Workflow、Validator 和媒体插件。

## 演示

Video Studio 的两条真实导出路径：对话式迭代构建视频世界，以及从单一 Brief 全自动一键生成长视频。GIF 预览只循环一小段；较长成片为便于在 GitHub 播放可能做了加速。

### 演示 1 — 交互式视频世界创作

五轮对话把一段电影感镜头从 15 秒扩展到 3 分钟。后续轮次在同一时间线上继续生成，而不是从头开始。每张 GIF 循环该轮结果中的一小段；第 5 轮是 3 分钟成片的加速预览。

#### 第 1 轮：15 秒

> 玻璃潮汐参考：《起源》——折叠城市 + 《奇异博士》——万花筒。在暮色中，一座由液态玻璃构成的十层高楼沿街道缓缓倾泻而下，映照出天际线，随后在扭曲中凝固成半透明结构。镜头：缓慢推入，35 毫米非变焦镜头，湿沥青路面，玻璃内嵌霓虹灯。无人物特写，无文字，无标志。IMAX 胶片，颗粒感，16:9 比例。

![15 秒玻璃潮汐液态玻璃城市镜头](docs/assets/demo/glass-tide-turn-1.gif)

#### 第 2 轮：30 秒

> 做一段 30 秒的视频，镜头连续。节奏要更动态、更抓眼球、更快。

![30 秒更快的连续玻璃潮汐镜头](docs/assets/demo/glass-tide-turn-2.gif)

#### 第 3 轮：45 秒

> 继续往后生成 15 秒，加一个美女天使飞过来。

![天使飞入玻璃潮汐时间线](docs/assets/demo/glass-tide-turn-3.gif)

#### 第 4 轮：60 秒

> 再生成 15 秒的视频，首帧用前面视频的尾帧。内容为相机跟上这个天使，然后往前面飞出现了一个天堂。

![相机跟上天使飞向天堂](docs/assets/demo/glass-tide-turn-4.gif)

#### 第 5 轮：3 分钟

> 继续生成，这个天使穿过远古、古代、近代和现代。最终生成 3 分钟版本。

为了方便播放，这里的预览 GIF 对完整 3 分钟成片进行了加速。

![加速预览天使穿越历史年代的 3 分钟成片](docs/assets/demo/glass-tide-turn-5.gif)

### 演示 2 — 一键自动化长视频

基于同一份电影 Brief（[《雨后》](docs/video-script/after-the-rain.zh.md)），Cuti Harness 自动规划镜头、执行生成并拼接成完整约 5 分钟预告片，中间无需手工改时间线。整条 Workflow 端到端全自动。

为了方便播放，这里的预览 GIF 对完整约 5 分钟成片进行了加速。

![加速预览全自动生成的《雨后》预告片](docs/assets/demo/demo-2-trailer.gif)

更多演示见 [Cuti Harness 官网](https://newai.land/cuti-harness#demos)。

## 架构

详见 [Cuti Harness 架构](docs/architecture.zh.md#cuti-harness-architecture)。

<a id="run"></a><a id="run-from-source"></a>

## 从源码克隆并运行（推荐）

这是开源分支的发布验收路径。环境准备命令负责安装依赖，启动命令负责运行 Video Studio、Cuti Harness、Video Runtime、Media Service 和 Sandbox Worker；无需 Docker、PostgreSQL 或 Redis。

### 前置条件

- Git。
- Node.js `22.19+` 或 `24+`。
- pnpm `11.x`。先运行 `pnpm --version`；如果尚未安装 pnpm，执行 `corepack enable`。
- Conda（Miniconda、Miniforge 或 Anaconda）。
- 环境准备阶段需要联网，以便安装 Python 依赖以及采用各自许可证的 FFmpeg/FFprobe 工具。

### 1. 克隆仓库

```sh
git clone https://github.com/VideoVerses/Cuti-Harness.git
cd Cuti-Harness
```

仓库只有 `main` 分支，Git 会自动检出该分支。

### 2. 创建并激活 Conda 环境

在仓库根目录执行：

```sh
conda env create --file environment.yml
conda activate cuti-video-agent
```

如果环境已经存在，改为执行：

```sh
conda env update --file environment.yml --prune
conda activate cuti-video-agent
```

后续命令都应在已激活的 `cuti-video-agent` 环境中执行。

### 3. 安装并构建 Node.js 依赖

在仓库根目录执行：

```sh
pnpm install --frozen-lockfile
pnpm run build
```

根目录构建会同时生成本地启动器需要的 Video Studio 产物。

### 4. 配置 OpenAI 和 WaveSpeed API Key

在仓库根目录复制配置模板。macOS 或 Linux：

```sh
cp config/.env.example .env
```

Windows PowerShell：

```powershell
Copy-Item config/.env.example .env
```

用文本编辑器打开仓库根目录中新建的 `.env`，将等号右侧替换成自己的真实 Key：

```dotenv
OPENAI_API_KEY=sk-your-real-openai-key
WAVESPEED_API_KEY=your-real-wavespeed-key
```

不要添加引号或在等号两侧添加空格，也不要提交 `.env`；该文件已被 Git 忽略。`OPENAI_API_KEY` 用于 Harness 的对话和规划模型，`WAVESPEED_API_KEY` 用于默认 Seedance 视频生成路径。`WAVESPEED_API_KEY` 为空时，新项目默认进入本地 Code-to-Video 模式；可以用输入区中的专用按钮为每个项目开启或关闭。若改用火山 Ark，可保留 `WAVESPEED_API_KEY` 为空、填写 `ARK_API_KEY`，并在需要提供方工作流时关闭 Code-to-Video。只有使用音乐生成能力时才需要 `SUNO_API_KEY`。修改 Key 后需重启本地服务栈才能重新加载。

### 5. 准备本地依赖

保持 `cuti-video-agent` Conda 环境处于激活状态，然后执行：

```sh
pnpm video:setup -- --data-dir .video-agent-harness-data
```

Conda 定义负责环境名称、Python 3.11 和 pip。环境准备命令会验证当前激活的正是该 Conda 环境，把固定版本的 Python 服务依赖安装进去，并把 FFmpeg/FFprobe 与固定版本的 HyperFrames 渲染器安装到 `.video-agent-harness-data`；它不会启动任何服务。Python requirements、本地渲染器版本发生变化，或改用新的数据目录后，需要重新运行该命令。

如果希望由环境准备命令下载经过校验和验证的便携 Python，可以显式传入 `--portable-python`。这是一条可选路径，不是默认行为：

```sh
pnpm video:setup -- --portable-python --data-dir .video-agent-harness-data
```

### 6. 启动完整本地服务栈

保持 `cuti-video-agent` Conda 环境处于激活状态，然后运行：

```sh
pnpm video:local -- --data-dir .video-agent-harness-data
```

启动命令只检查准备好的环境并启动服务，不会下载 Python 或安装依赖。保持终端运行。仓库内的数据目录已被 Git 忽略，还能避免部分 Windows 环境中的跨盘重命名错误。

使用可选便携环境时，启动命令需要传入相同选项：

```sh
pnpm video:local -- --portable-python --data-dir .video-agent-harness-data
```

当终端输出 `Cuti Harness is ready` 后，访问 [http://127.0.0.1:3000/#/zh/create](http://127.0.0.1:3000/#/zh/create)。Video Studio 没有登录流程，本地项目身份固定为 `local-user`。

### 7. 验证服务栈

在第二个终端中执行 `conda activate cuti-video-agent`，然后进入仓库根目录运行：

```sh
pnpm video:doctor -- --data-dir .video-agent-harness-data
curl http://127.0.0.1:8001/health
```

Doctor 应将包括 `Local HyperFrames renderer` 在内的每个组件显示为 `OK`，健康检查应返回 `{"status":"healthy"}`。端口 `3000` 是 Video Studio，`3080` 是 Cuti Harness，`8001` 是 Video Runtime，`8700` 是 Sandbox Worker，`18080` 是 Media Service。

使用便携环境时，`video:doctor` 也需要传入 `--portable-python`。

在启动终端按 `Ctrl+C` 即可停止。npm 本地模式面向可信的单用户机器，子进程 Worker 不构成安全隔离边界；需要 PostgreSQL、多用户运行或执行不受信任的插件时，请使用 Docker Compose 部署方式。

可以先用下面的低成本 Prompt 测试：

> 制作一个 10 秒、两个镜头的电影感视频：日出时，一个机器人给一朵花浇水。两个镜头保持同一个机器人，不要旁白，并导出最终 MP4。

## 开发模式

工具配置源文件位于 `config/root/`。`pnpm install` 会在根目录生成由 Git 忽略的配置入口，供 TypeScript、编辑器、测试和 Git 钩子使用。请编辑 `config/root/` 中的源文件；拉取配置更新后，或使用 `--ignore-scripts` 安装时，运行 `node scripts/materialize-root-configs.mjs`。

如果需要使用 Vite 热更新，请保持完整本地服务栈运行，然后在另一个终端执行：

```sh
cd apps/video-studio
cp ../../config/.env.example .env.local
```

Windows PowerShell 同样可以使用 `Copy-Item ../../config/.env.example .env.local`。在 `apps/video-studio/.env.local` 中设置：

```dotenv
VITE_VIDEO_RUNTIME_URL=http://127.0.0.1:8001
VITE_VIDEOCHAT_URL=http://127.0.0.1:8001
VITE_CUTI_BACKEND_URL=http://127.0.0.1:8001
VITE_BACKEND_URL=http://127.0.0.1:8001
```

Studio 没有登录流程。项目身份是 Video Runtime 的 `local-user`。

回到仓库根目录启动 Vite：

```sh
pnpm --filter @cuti-ai/video-studio run dev
```

访问 [http://127.0.0.1:5173/#/zh/create](http://127.0.0.1:5173/#/zh/create)。

## 常见问题

| 现象 | 检查项 |
| --- | --- |
| 页面能打开，但发送 Prompt 后没有响应 | 确认 Cuti Harness 仍在端口 `3080` 运行，并且仓库 `.env` 中存在 `OPENAI_API_KEY`。 |
| 出现 `401`、`NO_AUTH` 或模型认证错误 | 检查 `.env` 中的 `OPENAI_API_KEY`，然后重启本地服务栈以重新加载。 |
| 图片或视频生成失败 | 配置所选 Workflow 需要的 Provider Key；默认 Seedance 路径需要 `WAVESPEED_API_KEY` 或 `ARK_API_KEY`。 |
| Build 一直排队或 Runtime 不可用 | 执行 `pnpm video:doctor -- --data-dir .video-agent-harness-data`，并检查启动器终端。 |
| 端口已被占用 | 释放或映射端口 `3000`、`3080`、`8001`、`8700` 或 `18080`，并保持 URL 和代理配置一致。 |
| 首次安装出现 `EXDEV` 或 `cross-device` | 使用文档中的 `--data-dir .video-agent-harness-data`，且不要把数据目录指向另一个磁盘。 |
| 使用代理时模型、Provider 或首次依赖下载超时 | 在启动 Shell 中导出 `HTTP_PROXY`、`HTTPS_PROXY` 和 `NODE_USE_ENV_PROXY=1`，不要写入 `.env`，然后重启服务栈。本地启动器也会自动检测已启用的 Windows 用户代理。 |

## 测试

```sh
pnpm --filter @cuti-ai/video-studio run build
python -m unittest discover -s services/video-runtime/tests/video_runtime -v
```

仓库同时保留 DeepSeek Harness 的完整检查。更多说明见[开发文档](docs/development.zh.md)和[贡献指南](CONTRIBUTING.zh.md)。

## 使用须知

项目目前处于开发者预览阶段，可能出现不兼容变更。Provider 调用可能产生真实费用，请先使用短视频和低成本 Prompt 测试。

## 项目团队

- **项目负责人：** Yingqing He
- **核心贡献者：** Kai Sun, Songsong Wang, Pengjun Fang
- **联合负责人：** Yazhou Xing

## 许可证

本项目使用 [MIT License](LICENSE)。DeepSeek 与 Cuti 导入代码的来源记录在[源码来源说明](docs/source-provenance.zh.md)；第三方依赖及许可证见 [THIRD_PARTY_NOTICES.md](guides/THIRD_PARTY_NOTICES.md)。

## ⭐️ Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=VideoVerses/Cuti-Harness&type=Date)](https://star-history.com/#VideoVerses/Cuti-Harness&Date)
