# shovel

`shovel` 用于为 SWE-bench 风格的数据实例生成 Docker 环境配置，输出每个实例的：
- `dockerfile`
- `eval_script`
- `setup_scripts.setup_repo.sh`

项目会并发处理实例，自动克隆仓库、调用 Agent 生成配置。

## 安装

```bash
pip install -e .
```

## API 配置

```bash
export ANTHROPIC_BASE_URL=your_base_url
export ANTHROPIC_API_KEY=your_api_key
```

## 快速开始

```bash
shovel --input multi_docker_eval_test.jsonl --output docker_res.json
```

## 命令参数

```bash
shovel --help
```

常用参数：
- `--input`：输入数据，支持 `.json`、`.jsonl` 或 HuggingFace dataset 名称（必填）
- `--output`：输出结果 JSON 文件路径，默认 `docker_res.json`
- `--repo-dir`：仓库克隆目录，默认 `./repo`
- `--model`：Agent 使用的模型名
- `--max-workers`：并发实例数，默认 `4`
- `--max-turns`：单实例最大 Agent 轮数，默认 `50`
- `--log-dir`：轨迹日志目录，默认 `./logs`
- `--resume`：从已有输出文件续跑
- `--instance-ids`：只跑指定实例 ID（可传多个）
- `--start` / `--end`：按实例顺序切片运行（1-based）
- `--split`：当 `--input` 是 HuggingFace dataset 时指定 split
- `--verbose`：输出 debug 日志

## 输入格式

输入实例需包含至少这些字段：
- `instance_id`
- `repo`
- `base_commit`
- `problem_statement`
- `test_patch`
- `patch`

## 输出格式

输出是一个 JSON 对象，key 为 `instance_id`，value 结构如下：

```json
{
  "instance_id": "xxx",
  "dockerfile": "FROM ...",
  "eval_script": "#!/bin/bash ...",
  "setup_scripts": {
    "setup_repo.sh": "#!/bin/bash ..."
  }
}
```

## 运行说明

- 程序会在每个实例完成后立即落盘到 `--output`，中断后可配合 `--resume` 继续。
- `eval_script` 会确保包含 `OMNIGRIL_EXIT_CODE` 输出，以兼容评测框架判定逻辑。
