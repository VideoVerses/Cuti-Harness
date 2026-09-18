# Cuti Harness

English | [中文](README.zh.md)

![Cuti Harness](docs/assets/cuti-harness-hero.jpg)

[![Webpage](https://img.shields.io/badge/Webpage-111827?style=for-the-badge&labelColor=38BDF8)](https://newai.land/cuti-harness) [![MIT License](https://img.shields.io/badge/MIT%20License-8B7CF7?style=for-the-badge)](LICENSE)

Cuti Harness is a general-purpose, open-source, plugin-based video harness for long-horizon video creation built on [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness). The harness owns the conversation, agent loop, and high-level tool selection. The video Runtime owns durable projects, Skills, artifact dependencies, incremental builds, timelines, validation, and exports.

This repository includes the Cuti Harness agent algorithm and loop, the Video Runtime backend, the Video Studio frontend, a compatibility BFF, Media Service, Sandbox Worker, and the provider, workflow, validator, and media plugins they use.

## Demo

Two real exports from Video Studio: iterative world building in conversation, and a fully automated one-shot long video from a single brief. GIF previews loop short clips; long exports may be sped up for GitHub playback.

### Demo 1 — Interactive video world creation

Five conversation turns grow one cinematic clip from 15 seconds to 3 minutes. Later turns extend the same timeline instead of starting over. Each GIF loops a short clip from that turn; turn 5 is a sped-up view of the 3-minute export.

#### Turn 1 — 15 seconds

> Glass Tide Reference: Inception - Folded City + Doctor Strange - Kaleidoscope Photoreal city avenue at blue hour. A ten-story wave of liquid glass rolls down the street towards the camera, reflecting the skyline, then freezes mid-twist into a translucent structure. Camera: slow push-in, 35mm anamorphic, wet asphalt, neon in the glass. No close-ups of people, no text, no logos. IMAX, film grain, 16:9.

![15-second Glass Tide liquid-glass city shot](docs/assets/demo/glass-tide-turn-1.gif)

#### Turn 2 — 30 seconds

> Make a 30-second video with continuous shots. It should be dynamic, more eye-catching and faster.

![30-second faster continuous Glass Tide shot](docs/assets/demo/glass-tide-turn-2.gif)

#### Turn 3 — 45 seconds

> Continue for another 15 seconds, and add a beautiful angel flying in.

![Angel flying into the Glass Tide timeline](docs/assets/demo/glass-tide-turn-3.gif)

#### Turn 4 — 60 seconds

> Generate another 15 seconds of video. Use the last frame of the previous video as the first frame. The camera follows this angel, then flies forward, and a heaven appears.

![Camera follows the angel into heaven](docs/assets/demo/glass-tide-turn-4.gif)

#### Turn 5 — 3 minutes

> Continue generating. This angel flies through prehistoric, ancient, early-modern, and contemporary eras. Export a 3-minute version.

For easier playback, the preview GIF is sped up from the full 3-minute export.

![Sped-up 3-minute flight through historical eras](docs/assets/demo/glass-tide-turn-5.gif)

### Demo 2 — One-shot automated long video

From one cinematic brief ([After the Rain](docs/video-script/after-the-rain.md)), Cuti Harness plans shots, runs generation, and assembles a complete ~5-minute trailer with no manual timeline edits between steps. The workflow is fully automated end to end.

For easier playback, the preview GIF is sped up from the full ~5-minute export.

![Sped-up Beyond the Rain automated trailer](docs/assets/demo/demo-2-trailer.gif)

More demos: see the [Cuti Harness webpage](https://newai.land/cuti-harness#demos).

## Architecture

See [Cuti Harness architecture](docs/architecture.md#cuti-harness-architecture).

<a id="run"></a><a id="run-from-source"></a>

## Clone and run from source (recommended)

This is the release acceptance path for the open-source branch. The setup command prepares dependencies, and the start command runs Video Studio, Cuti Harness, Video Runtime, Media Service, and Sandbox Worker. Docker, PostgreSQL, and Redis are not required.

### Prerequisites

- Git.
- Node.js `22.19+` or `24+`.
- pnpm `11.x`. Run `pnpm --version`; if pnpm is missing, run `corepack enable`.
- Conda (Miniconda, Miniforge, or Anaconda).
- Internet access during environment setup so the setup command can install Python packages and separately licensed FFmpeg/FFprobe tools.

### 1. Clone the repository

```sh
git clone https://github.com/VideoVerses/Cuti-Harness.git
cd Cuti-Harness
```

The repository has one branch, `main`, which Git checks out automatically.

### 2. Create and activate the Conda environment

Run from the repository root:

```sh
conda env create --file environment.yml
conda activate cuti-video-agent
```

If the environment already exists, run:

```sh
conda env update --file environment.yml --prune
conda activate cuti-video-agent
```

Run every subsequent command with the `cuti-video-agent` environment activated.

### 3. Install and build the Node.js dependencies

Run from the repository root:

```sh
pnpm install --frozen-lockfile
pnpm run build
```

The root build also produces the Video Studio bundle required by the local launcher.

### 4. Configure the OpenAI and WaveSpeed API keys

Copy the configuration template from the repository root. On macOS or Linux:

```sh
cp config/.env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item config/.env.example .env
```

Open the new repository-root `.env` in a text editor and replace the values after each equals sign with your real keys:

```dotenv
OPENAI_API_KEY=sk-your-real-openai-key
WAVESPEED_API_KEY=your-real-wavespeed-key
```

`OPENAI_API_KEY` powers the Harness conversation and planning model, while `WAVESPEED_API_KEY` powers the default Seedance video path. When `WAVESPEED_API_KEY` is empty, new projects default to the local Code-to-Video mode; the dedicated composer button can enable or disable it for each project. To use Volcengine Ark instead, leave `WAVESPEED_API_KEY` empty, set `ARK_API_KEY`, and disable Code-to-Video when you want the provider workflow. `SUNO_API_KEY` is needed only for music generation. Restart the local stack after changing a key so it is reloaded.

### 5. Prepare the local dependencies

Keep the `cuti-video-agent` Conda environment activated, then run:

```sh
pnpm video:setup -- --data-dir .video-agent-harness-data
```

The Conda definition owns the environment name, Python 3.11, and pip. The setup command verifies that this exact Conda environment is active, installs the pinned Python service dependencies into it, and installs FFmpeg/FFprobe plus the pinned HyperFrames renderer under `.video-agent-harness-data`. It does not start any service. Run it again after the Python requirements or local renderer version changes, or when using a new data directory.

To let setup download a checksum-verified portable Python instead of using an existing Python environment, pass `--portable-python`. This is an explicit alternative, not the default:

```sh
pnpm video:setup -- --portable-python --data-dir .video-agent-harness-data
```

### 6. Start the complete local stack

Keep the `cuti-video-agent` Conda environment activated, then run:

```sh
pnpm video:local -- --data-dir .video-agent-harness-data
```

The start command only checks the prepared environment and starts services; it does not download Python or install dependencies. Keep this terminal open. The repository-local data directory is ignored by Git and avoids cross-volume rename errors on some Windows installations.

When using the optional portable environment, pass the same option to start:

```sh
pnpm video:local -- --portable-python --data-dir .video-agent-harness-data
```

When the terminal prints `Cuti Harness is ready`, open [http://127.0.0.1:3000/#/zh/create](http://127.0.0.1:3000/#/zh/create). Video Studio has no login flow; local project identity is `local-user`.

### 7. Verify the running stack

In a second terminal, run `conda activate cuti-video-agent`, then run from the repository root:

```sh
pnpm video:doctor -- --data-dir .video-agent-harness-data
curl http://127.0.0.1:8001/health
```

Doctor should report every component, including `Local HyperFrames renderer`, as `OK`; the health response should be `{"status":"healthy"}`. Port `3000` is Video Studio, `3080` is Cuti Harness, `8001` is Video Runtime, `8700` is Sandbox Worker, and `18080` is Media Service.

Portable-environment users also pass `--portable-python` to `video:doctor`.

Stop the stack with `Ctrl+C` in its terminal. Local npm mode is intended for a trusted single-user machine: its subprocess worker is not a security isolation boundary. Use Docker Compose deployment for PostgreSQL, multi-user operation, or untrusted executable plugins.

Try a low-cost first prompt such as:

> Create a 10-second, two-shot cinematic video of a robot watering one flower at sunrise. Use the same robot in both shots, no narration, and export the final MP4.

## Development mode

Tool configuration sources live in `config/root/`. `pnpm install` generates the Git-ignored root entrypoints required by TypeScript, editors, tests, and Git hooks. Edit the source files there, and run `node scripts/materialize-root-configs.mjs` after pulling configuration updates or when installing with `--ignore-scripts`.

To run Video Studio with Vite hot reload, keep the complete local stack running, then use another terminal:

```sh
cd apps/video-studio
cp ../../config/.env.example .env.local
```

Set these values in `apps/video-studio/.env.local`:

```dotenv
VITE_VIDEO_RUNTIME_URL=http://127.0.0.1:8001
VITE_VIDEOCHAT_URL=http://127.0.0.1:8001
VITE_CUTI_BACKEND_URL=http://127.0.0.1:8001
VITE_BACKEND_URL=http://127.0.0.1:8001
```

Studio has no login flow. Project identity is `local-user` in Video Runtime.

Return to the repository root and start Vite:

```sh
pnpm --filter @cuti-ai/video-studio run dev
```

Open [http://127.0.0.1:5173/#/zh/create](http://127.0.0.1:5173/#/zh/create).

## Common problems

| Symptom | Check |
| --- | --- |
| The UI opens but sending a prompt has no response | Confirm Cuti Harness is still running on port `3080` and `OPENAI_API_KEY` is present in the repository `.env`. |
| `401`, `NO_AUTH`, or model authentication error | Check `OPENAI_API_KEY` in `.env`, then restart the local stack so the launcher reloads it. |
| Video or image generation fails | Configure the provider key required by the selected workflow; the default Seedance path needs `WAVESPEED_API_KEY` or `ARK_API_KEY`. |
| Build remains queued or the Runtime is unavailable | Run `pnpm video:doctor -- --data-dir .video-agent-harness-data` and inspect the launcher terminal. |
| Port is already in use | Free or remap ports `3000`, `3080`, `8001`, `8700`, or `18080`. Keep URLs and proxy settings consistent. |
| `EXDEV` or `cross-device` appears during first-run installation | Use the documented `--data-dir .video-agent-harness-data` command and do not point the data directory at another drive. |
| Model, provider, or first-run dependency download times out behind a proxy | Export `HTTP_PROXY`, `HTTPS_PROXY`, and `NODE_USE_ENV_PROXY=1` in the launching shell, not in `.env`, then restart the stack. The launcher detects an enabled Windows user proxy automatically. |

## Tests

```sh
pnpm --filter @cuti-ai/video-studio run build
python -m unittest discover -s services/video-runtime/tests/video_runtime -v
```

The repository also retains the broader DeepSeek Harness checks. See [development](docs/development.md) and [contributing](CONTRIBUTING.md).

## Usage notice

This project is in developer preview and can introduce compatibility-breaking changes. Provider calls may incur real costs. Start with short videos and low-cost test prompts.

## Project team

- **Project Lead:** Yingqing He
- **Core Contributors:** Kai Sun, Songsong Wang, Pengjun Fang
- **Co-Lead:** Yazhou Xing

## License

This project is licensed under the [MIT License](LICENSE). DeepSeek and imported Cuti provenance is documented in [Source Provenance](docs/source-provenance.md). Third-party dependencies and licenses are listed in [THIRD_PARTY_NOTICES.md](guides/THIRD_PARTY_NOTICES.md).

## ⭐️ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=VideoVerses/Cuti-Harness&type=Date)](https://star-history.com/#VideoVerses/Cuti-Harness&Date)
