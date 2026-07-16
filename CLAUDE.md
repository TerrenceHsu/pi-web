# CRITICAL — 必读约定（最高优先级）

> **本目录是 `D:\LLMTutorial\pi\pi-py` 的精简副本**——
> 仅保留 **Step 1–21**（core 15 步 + MCP / Permission / Skill Loader / Web App / Provider）。
>
> 主仓库（含全部 Step）：`D:\LLMTutorial\pi\pi-py\`

## 项目运行环境：conda 环境 `pipy`

**本项目所有 Python 命令必须在 conda 环境 `pipy` 中运行。**

> ⚠️ **注意**：`pipy` 环境已经通过 `pip install -e` 安装了主仓库
> `D:\LLMTutorial\pi\pi-py\`。在测试本副本时**必须用 `PYTHONPATH=src`
> 覆盖**，否则会导入主仓库的代码：
>
> ```bash
> cd D:/LLMTutorial/test
> PYTHONPATH=src /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow"
> ```
>
> 或先 `pip uninstall pi-agent-core-py` 再 `pip install -e .[dev]` 本副本。

- Conda 安装路径：`D:\miniconda`
- 环境名：`pipy`
- Python 版本：3.12
- 环境 Python 路径：`D:\miniconda\envs\pipy\python.exe`
- 环境 pip 路径：`D:\miniconda\envs\pipy\Scripts\pip.exe`

### 执行规则（每次都做）

1. **运行任何 Python 命令前必须先激活环境**。在 bash 里用绝对路径调用，避免依赖 PATH：

   ```bash
   # 推荐：直接用环境内的可执行文件
   /d/miniconda/envs/pipy/python.exe <script>.py
   /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v
   /d/miniconda/envs/pipy/python.exe -m pip install -e ".[dev]"

   # 或通过 conda activate（需要先 source conda 初始化脚本）
   source /d/miniconda/etc/profile.d/conda.sh
   conda activate pipy
   python <script>.py
   ```
2. **不要用系统 Python**（`/c/Users/Administrator/AppData/Local/Programs/Python/Python312/python.exe`）
   不要用 `py`、不要用其它 conda 环境（`llm-base` / `medix` / `wiki` / `finclaw`）。
3. **安装依赖**时用：

   ```bash
   /d/miniconda/envs/pipy/python.exe -m pip install -e ".[dev]"
   ```
4. **测试**时用：

   ```bash
   /d/miniconda/envs/pipy/python.exe -m pytest tests/ -v -m "not slow"
   ```

### 项目快速参考

- 项目目录：`D:\LLMTutorial\pi\pi-py`
- 源码：`src/pi_agent_core_py/`
- 测试：`tests/`
- 示例：`examples/`
- 实施计划：`PLAN.md` + `steps/step-XX-name/GUIDE.md`
- 上游 TS 项目：`D:\LLMTutorial\pi\pi-main\`（参考用，不要改）
- 环境变量：`.env`（已配智谱 GLM-5.0 的 Anthropic 兼容凭证）

---

# 项目说明

`pi-agent-core-py` 是 `@earendil-works/pi-agent-core`（TypeScript 上游项目）的 Python 完整移植。

> 上游 TypeScript 项目路径（不在本副本）：`../pi-main/packages/agent`

## 进度

参见 `PLAN.md`。当前在 Step 0（脚手架）完成，准备进入 Step 1（类型层）。

## 开发流程

1. 每完成一个 Step，按 `steps/step-XX-name/GUIDE.md` 实现
2. 在 `PLAN.md` 的进度表把 ☐ 改成 ✅
3. 跑 `pytest tests/ -v -m "not slow"` 确认不回归
4. 不依赖真实 API key 的测试必须始终通过；slow 标记的测试在 `PI_RUN_SLOW=1` 时才跑

## 不要做的事

- 不要修改 `pi-main/` 下的 TS 源码（那是上游参考项目）
- 不要把 `.env` 提交到 git（已在 `.gitignore`）
- 不要在 Pydantic 模型上做 mutation 后又传给订阅者——保持不可变语义
- 不要在循环里抛异常——按 TS 版契约，错误要包成 `stop_reason="error"` 的 AssistantMessage 或 `ErrorEvent`
