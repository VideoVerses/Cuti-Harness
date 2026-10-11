# Video Runtime

审核失败会将提交的 prompt、输入参考素材、供应商错误及持久化的替换历史交给 Agent。每条失败任务链只允许一次自动合规修正，用于澄清歧义或移除用户未要求的扩写，同时保留参考素材、模型及供应商。重复被拒绝的 prompt、审核未解决时未关联替换的生成任务，以及修复再次被拒绝后的继续尝试都会被拦截。修复耗尽会取消待执行的下游任务并保留已完成产物；Studio 显示审核、修复、恢复成功或需要用户修改的状态。核心剧情保留由 Agent 指令约束，代码不保证语义等价。实质内容或参考素材变更需要用户修改需求；checkpoint 重投递和重启不会重置自动修复预算。

Harness 回合失败时，Studio 事件 API 会提供模型服务的错误消息及错误码。现有错误提示区会显示 API 额度耗尽等模型错误，回放会话历史时也适用。

启用分阶段和持续规划后，产物重生成、结构化编辑和后期编辑预览会创建续作目标，保留当前选中产物及原始编辑要求。DeepSeek 在绑定的同一个 Session 中编写可执行 PlanPatch；编辑预览不重建固定 Workflow DAG，也不自动把待编辑视频当作 Provider 参考视频。任务终态失败先反馈给 Agent，再决定下一次生成提交。编辑完成需要产生新产物，不能仅凭已有视频声明完成。关闭持续规划时保留确定性兼容路径。

`cinematic` Workflow 复用 Seedance2 的自主规划，使用 `reference_mode: multi_reference`：续拍保留原始身份／场景图，并把上一段真实尾帧追加为普通参考图，不锁定首帧。选择 Cinematic 或输入 `$cinematic` 使用；已有 Seedance2 项目不会自动切换。

Seedance 续拍规划将上一段尾帧保存在 `start_image_from_step`，将最初的身份／场景参考图保存在 `reference_from_steps`。WaveSpeed 已公布的 I2V 请求没有普通参考图字段，因此同时要求严格首帧和参考图的新请求会在提交前被拒绝，不再静默丢弃身份约束。已提交的远程任务仍可继续查询。实际执行需要支持组合输入的接口，或用户明确同意不保证严格首帧的多参考模式；Runtime 不会私自更换 Provider 或模式。

字幕转写支持通过 `prompt` 提供已知歌词或名称作为识别上下文。转写含至少八个词且超过一半词的起止时间重合时，会在字幕渲染前被拒绝；Agent 可补充音频上下文后通过 PlanPatch 替换任务。该时间戳检查用于识别一种异常模式，不代表字幕内容已通过准确性验收。

独立启动入口默认开启分阶段规划和持续 PlanPatch，可通过环境变量显式覆盖。Windows 可使用 `scripts/start-video-runtime.ps1`，通过 `-Python`、`-CredentialEnvFiles`、`-Port` 和 `-MediaServiceUrl` 指定运行环境；媒体服务地址默认为 `http://127.0.0.1:18080`。脚本在导入凭据后应用本地服务配置，并在启动前检查媒体服务就绪状态，防止旧环境文件把本地构建转向旧服务地址。

Provider 分发将任务输入 ArtifactVersion 转换为有序的图片、视频和音频 URI 列表，直调与 Atomic 路径共用。显式媒体参数顺序优先，其余任务输入按声明顺序去重合并。引用素材缺失、提示词索引越界时在提交前失败。输出产物记录解析后的输入版本与具体参数。旧调用把 Seedance 模型名放在 `provider` 字段时，分发会拆分为 WaveSpeed 通道和精确的模型版本；相互冲突的版本参数在提交前失败。分发保留远程任务 ID 和幂等键。即使调用方遗留了 `i2v` 模式，普通参考图仍只作为参考；只有显式 `start_image_url`、首帧别名，或提示词中明确标为首帧的图片槽位才会启用严格图生视频。

失败任务可通过 PlanPatch 替换映射提交新参数。待执行的下游任务会复制并改接依赖，旧下游步骤在同一版本事务中取消。失败步骤保留原参数和 `superseded_by` 指针；整单重试跳过已替代及已取消步骤。已开始执行的下游不能自动改接。失败的持续 Build 在查看修复检查点时仍保持失败，只有替换补丁通过计划、规格与项目版本校验并提交后才恢复。

Create 的停止操作同时取消 DeepSeek 对话和活动 Runtime 构建。普通 Worker 写入不能把已停止、失败或完成的 Build 或 BuildStep 改成其他状态，终态 Build 不能发布作品版本；只有仓库的显式恢复与重试操作可以重新开启已停止或失败的任务。停止后会保留草稿、待执行步骤与远程任务 ID；已提交的外部任务可能继续执行并计费。发送后续消息前需明确确认恢复。恢复会继续最新的已停止构建，检查原项目版本、复用已完成步骤并查询原远程任务，而非创建替代构建。刷新页面不会自动恢复。

[English](README.md) | 中文

Python Video Runtime 独立于 agent Session 或 Run 管理长期视频项目。它存储不可变作品版本、带版本产物、类型化产物依赖、变更请求、重建计划、持久 Build、校验结果、时间线、导出和项目内单调递增事件。

## 运行

独立进程安装与 Runtime 镜像相同的依赖，来源是 `pyproject.toml`：

```sh
python -m pip install -e .
python -m uvicorn app.video_runtime.standalone:app --host 127.0.0.1 --port 8001
```

设置 `VIDEO_RUNTIME_DATABASE_URL` 可启用 Postgres 持久化。未设置时，独立服务使用 `VIDEO_RUNTIME_LOCAL_STATE_PATH` 指定的原子 JSON 存储；该变量未设置时，路径与 `@cuti-ai/video-agent-harness` 使用的每用户系统数据目录一致。如果规范路径尚不存在，但检测到旧版工作目录相对状态文件，服务会停止启动并报告来源和迁移目标，而不是创建空项目库。只有隔离测试才应设置 `VIDEO_RUNTIME_IN_MEMORY=true`。`deploy/compose.video.yml` 会在启动服务前按顺序执行 `migrations/video_runtime` 中的全部迁移。

使用本地存储时，独立入口默认将 `PUBLIC_BASE_URL` 设置为 `http://127.0.0.1:8001`，与文档端口及 `/files/*` 挂载一致；若 Runtime 使用其他域名或端口，需要显式覆盖。发往远程生成服务的媒体仍会经过 media-egress 上传，不会直接把回环地址交给 Provider。

## Runtime API

失败任务修复同步重写明确的任务引用字段和调度依赖，包括有序视频输入、音频及字幕来源、生成参考素材引用。提示词、URL 和产物版本 id 不会被重写。PlanPatch 提交时检查引用是否指向未被替换的任务，并在执行前补齐调度依赖；不存在或已被替换的目标会被拒绝。修复不会修改已经持久化的历史计划。

`/api/video` 下的版本化 API 提供项目创建与查看、统一编辑工作区、结构化编辑与确定性依赖影响预览、Build 提交与取消、产物版本选择、作品版本恢复、固定作品版本导出、已配置插件管理和项目事件流。修改操作可能因重放产生重复工作时，必须携带幂等键。Build、选择、恢复和导出会在修改权威状态前比较基础作品版本。

## 插件执行

同时启用分阶段规划和 `VIDEO_CONTINUOUS_PLAN_PATCH_ENABLED` 时，每个 Build 由一个 Runtime Worker 调度。Worker 按配置的并发上限启动依赖已完成的任务，在单任务完成、最终失败或收到 PlanPatch 时重新读取持久化计划。单任务结束即可请求 Agent 规划，不等待无关 Provider 操作。同一 Build 只保留一个待处理检查点；规划期间发生的后续结束事件合并到下一份快照，通过绑定的 DeepSeek Session 投递，不增加第二套 Planner。失败任务保留记录，由 Agent 使用新任务 ID 提交替代任务。

执行期间可调用 `POST /api/video/projects/{project_id}/builds/{build_id}/checkpoints/live` 打开或复用规划快照，然后携带返回的检查点 ID、准确的 Plan/Spec 版本和幂等键提交修改。PlanPatch 可以追加依赖已有任务或同批新增任务的步骤，也可以取消待执行任务及其待执行后继。任务启动与取消原子比较已存储的 Step 状态；不能借待执行任务取消操作取消运行中的任务。有活动任务时禁止宣告目标完成。服务退出保留远程任务 ID 用于对账；恢复时即使 Build 仍在等待 Agent，也会继续追踪远程任务。同一仓库只能由一个 Runtime 进程执行任务，尚不提供分布式 Worker 租约。

`video-plugin.yaml` 声明 Provider、Workflow、Style、Validator 和 Media 贡献，以及依赖与权限。`VIDEO_PLUGIN_PATHS` 可以用平台分隔的目录替换内置 `plugins` 根目录，这些目录的直接子目录包含 manifest。插件可以继承 `BaseVideoPlugin`；其 `capability_handlers()` 将声明的 Capability id 映射到可执行 Handler。受信任的内置插件可以进程内加载；不受信任的插件通过 `sandbox_runtime` 声明镜像、入口和超时，运行时不会导入它的模块，而是带着签名 Grant 的限制交给 `services/sandbox-worker` 执行。

`VIDEO_SKILL_PATHS` 可以替换进程级 Skill 根目录。未配置时，单一 `VideoSkillRuntime` 会发现内置 system、builtin、external 和 stage-director Skill。共享 Catalog 校验可执行契约和 Workflow 声明，在每个已规划 Build 步骤上冻结辅助 Skill 上下文，并通过 `cuti.skill-workflows` 注册 Workflow 贡献。`GET /api/video/workflows` 同时返回 manifest 工作流与最终生效的 Skill 工作流。

已安装的 Workflow Skill 可以声明 `workflow.planning.mode: agentic` 和非空的 `allowed_capabilities` 列表，使用 DeepSeek 编写的 PlanPatch 执行，无需按名称绑定编译器。必须同时启用 `VIDEO_STAGED_PLANNING_ENABLED` 和 `VIDEO_CONTINUOUS_PLAN_PATCH_ENABLED`。Catalog 返回 `executionKind: agent_plan_patch`；DeepSeek 加载原始 Skill 指令并提交结构化任务，不生成可执行编译器代码。具名 Cuti 编译器保留原有元数据检查。其他规划模式需要专用编译器；缺少执行支持时显示不可用，不回退到其他 Workflow。

自主规划 Skill 可以声明 `workflow.parameters.completion_artifact_types`，例如纯文档 Workflow 使用 `[script]`，默认值为 `[video]`。完成要求存在新建且已完成的指定类型交付产物，同时没有活动任务；来源素材和意图记录不能满足此条件。上传包含与 Skill 同名目录及其 `SKILL.md` 的 ZIP；安装复用 Cuti 的压缩包校验并重新加载共享 Catalog 与 Resolver。Capability 参数校验、DAG 校验、项目访问控制、签名 Grant 和执行限制仍然强制生效。Skill 能力白名单只能限制能力，不能授予凭据或网络访问权限。

内置适配器复用 Cuti 的文本、图片、音乐、视频、TTS、FFmpeg、字幕与 Lipsync 操作。可选工作流（`mv`、`seedance2`、`short-drama-workflow`、`cuti-product-workflow` 以及 product-workflow 变体）编译与 Provider 无关的 `VideoSpec`；`cuti.style-presets` 在编译前应用已安装的提示词默认值。生产身份使用独立 Runtime 的服务 Bearer Token，未配置时默认拒绝请求。本地自托管使用 `local-user`。

每个新的 `ProjectIntent` 和 `VideoSpec` 都持久化一份语言契约，分别记录界面、用户可见内容、对白或旁白、字幕和 Provider 提示词语言。BFF 会把该契约注入首次规划、后续编辑和自动检查点回合；Runtime 生成的用户产物会记录契约，并拒绝明确的文本语言不匹配。只有 `language` 字段的旧文档会为所有内容字段补上相同语言的默认值。

所有已注册 Capability 都通过 `RuntimeCapabilityRegistry` 和 `CapabilityExecutionGateway` 执行。网关校验服务端签名 Grant，Grant 绑定项目、Session、用户、插件、Capability、域名、费用上限、超时、并发、重试、幂等键、审计 id 和过期时间。进程内可执行插件要求服务端持有至少 32 字节的 `VIDEO_CAPABILITY_GRANT_SECRET`。

场景参考图会同时检查 Provider 输入边界和最终渲染像素。场景参考图校验失败时，会在任何依赖它的视频片段执行前进入分阶段构建唯一一次语义修复。`VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED` 可显式启停像素检查；未设置时，只要配置了 `OPENAI_API_KEY` 就会自动启用。`VIDEO_SCENE_REFERENCE_VALIDATOR_MODEL` 用于指定支持视觉输入的校验模型。

## 兼容 API

已绑定项目的 `thread_id` 就是持久化 Harness Session id。后续消息拒绝冲突的线程 id，并追加到该 Session，保留对话、工具结果及 Harness 压缩摘要。每条后续消息记录当前项目版本、最新 Build 和检查点引用；顺序规划通过 Runtime 工具读取真实的前序产物，以其版本 id 建立依赖。Runtime 检查点持久化执行进度，并将续跑消息排入同一 Session；它们不是 LangGraph 检查点，也不是独立的 agent loop（智能体循环）。重启必须同时保留 Harness 数据目录和 Runtime 存储。

Studio 对接 `app.video_runtime.standalone:app`。`/chat-v1/service` 挂载 DeepSeek BFF，它是唯一的规划循环。

如果粘贴的 Create Space URL 指向一个仍存在于 DeepSeek、但在恢复后的 Runtime 中已没有 Project 绑定的 Session，BFF 会创建全新 Session 并返回新的 `thread_id`，不会把新 Project 接到未绑定的历史 Session 上。

## 测试

产物重新生成从当前选中版本及其生成计划恢复 Capability 和参数，合并用户补丁并重建硬依赖下游，无需完整 VideoSpec。工作区规格按已提交 ProjectVersion 的 Revision 读取。持续 PlanPatch 将执行配方存入规格修订；后续创作保留已选生成素材供 Agent 复用。运行中支持 live 补丁和取消待执行任务，已完成产物通过新 Build 修改，失败时保留原作品版本。

检查点投递根据已消费的 Session 消息和已结束的回合进行对账。回合结束却未处理检查点时，在原有投递次数限制内重试；重试和检查点查询均包含当前任务结果。排队中的消息、活跃回合、已处理检查点和已取消 Build 不会被判定为缺少确认。投递中断仍通过租约恢复。

```sh
python -m unittest discover -s tests/video_runtime -v
```

`tests/` 下还有 Capability、媒体工具和 DeepSeek BFF 的 pytest 覆盖。

实时编辑快照属于发起请求的 Agent 回合，不参与检查点自动投递。空的 agentic 确认只解决当前检查点，不推进计划或规格版本。真实任务完成和失败仍自动通知；后续用户编辑可以打开新的实时快照。

多次编辑支持在一个 PlanPatch 中串联媒体操作，并为同一来源生成多个版本。与项目产物一致的冗余 URL 会归一化为结构化输入。持久化执行历史不参与创作 VideoSpec 校验。

对白字幕使用实际音频转写，不使用剧本提示。Cuti HyperFrames 模板读取持久化转写片段，负责位置和时间轴；默认保留原声语言。显式翻译通过逐片段的 `translated_texts` 提供，保留原时间戳。无需 Workflow 的 PlanPatch 支持原子视频生成并接入拼接。用户提出编辑请求后，从预览直接执行，不再二次确认；排队中的工作必须跟进至用户要求的产物生成。

Code-to-Video 是独立且持久化的运行模式，不是普通工作流能力。Studio 提供专用开关，并且仅在 `WAVESPEED_API_KEY` 为空时让新项目默认进入该模式。该模式中的视频轮次只生成一个自包含的 `media.hyperframes_render` PlanPatch 操作；普通聊天不执行媒体操作。持久化模式关闭时 Runtime 拒绝该能力，而且它不能与 Planning-off 同时启用。
