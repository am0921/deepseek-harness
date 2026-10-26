# Dsh Arena

[English](README.md) | 中文

## 概述

使用相同的 Dsh PTC 配置与任务，并排运行 DeepSeek 官方 API 和 Engy。每个面板嵌入原有的 Dsh Web UI，外层控件显示用量、速度、延迟、预估费用和评分结果。完整操作约定见[日文指南](GUIDE.ja.md)。

## 目录

- [本地运行](#run-locally)
- [公平性与评分](#fairness-and-grading)
- [验证范围](#verification-scope)

<a id="run-locally"></a>
## 本地运行

安装 `uv` 并启动 Docker Desktop，为其虚拟机提供至少 5 个 vCPU 和 4 GiB 内存。

```sh
./apps/arena/setup.sh
./apps/arena/run.sh --open
```

打开 `http://127.0.0.1:8765`，在连接设置中填写密钥，再点击同时开始按钮。控制器也会从环境变量、Git 忽略的根目录 `.env` 或 `.arena/keys.env` 读取 `DEEPSEEK_API_KEY` 和 `ENGY_API_KEY`。可选的密钥保存文件使用 0600 权限；agent（智能体）只获得本次运行专用的网关凭据。

演示通过确定性的本地提供方执行真实的 Dsh、PTC、Docker、评分、修复与再次评分。API 用量与费用会明确标为模拟值。

```sh
./apps/arena/run.sh --port 8775 --demo
./apps/arena/verify.sh
```

停止操作会释放保留的 agent 容器。运行记录与报告存放在 `.arena/runs/` 下。修改已验证的源码后，需要再次验证并重启控制器才能开始实验。

<a id="fairness-and-grading"></a>
## 公平性与评分

两侧使用相同且固定的 agent 镜像及 PTC 设置。屏障只有在两个初始请求除模型 ID 外的 JSON 完全一致后才会转发。UI 显示实际发出时间差；无法同步不同提供方的请求到达时间。

每个 agent 获得互不重叠 CPU 集合上的 2 个 vCPU、禁用 swap 的 1536 MiB 内存、128 个进程及 256 MiB tmpfs 工作目录。容器创建后会检查 Docker 设置。提供方硬件、缓存状态和负载不受本控制器控制。

每侧使用独立的内部网络。其唯一的对外通信通道是转发到经过认证的模型网关的固定目标中继，上限为 0.25 vCPU 和 64 MiB，并禁用 IP 转发。agent 无法直接连接主机操作 API 或另一侧。真实 Docker 测试同时检查模型通信成功，以及通过 IP 地址直接访问主机操作 API 被阻止。

每次 SDK 轮次完成时，控制器会暂停 agent、创建文件快照，再运行独立且断网的评分器，其上限为 0.5 vCPU 和 256 MiB。待测服务器使用与受保护评分器不同的 UID。测试集包含 23 项，其中包括 1600 个确定性生成操作；四项独立检查可并行执行。原始 JSON 报告返回同一 Session，agent 在配置的上限内继续修复。通过后，agent 确认结果，控制器再检查已评分文件保持不变。

Token/s 使用 API 报告的输出 token 数与内容 delta 时间计算。Ping 是经过认证的模型列表 HTTP 延迟。缓存输入及占比使用 API 报告的缓存读取 token 数。费用按照可修改的每百万 token 美元单价估算，初始值为[官方高峰价格](https://api-docs.deepseek.com/quick_start/pricing/)和 [Engy 价格](https://engy.ai/pricing)；用量不完整时会保留标记。

<a id="verification-scope"></a>
## 验证范围

Lean 4.35.0 检查 18 个模型定理，涵盖放行条件、状态转换、资源划分、TTL 与整数规则和缓存计费。其枚举的转换表必须与 Python 运行时使用的表相同，不允许 `sorry` 或自定义公理。属性测试、真实 Redis 对照轨迹、通过的对照实现、六类缺陷实现的拒绝及真实 Docker 快照检查分别验证实现。

这些证明覆盖所述数学模型，不证明完整 Python 实现、操作系统、TCP、Docker、上游模型或任意提交程序的正确性。有限测试集不等于对所有可能程序的完整证明。验证清单记录源码哈希；源码改变后，开始按钮会失效，直到重新验证。
