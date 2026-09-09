# 飞书多维表格（bitable）API 限制备忘

> 调研时间：2026-09-09
> 调研者：subagent #24（build_bitable_charts）+ main session
> 调研对象：飞书 open platform bitable 相关 API
> 结论：**3 个真实限制**（不是脚本 bug），影响自动化建图能力

---

## 限制 1：Chart 创建 API 完全没有公开

### 现象
按官方文档里的 endpoint 试了 30+ 种路径，全部返回 `404 page not found`：

```
POST /bitable/v1/apps/{app}/tables/{table}/views/{view}/charts          ← 文档里的
POST /bitable/v1/apps/{app}/tables/{table}/views/{view}/chart           ← 单数
POST /bitable/v1/apps/{app}/dashboards/{...}                            ← dashboards
POST /bitable/v1/apps/{app}/widgets/{...}                               ← widgets
POST /bitable/v1/apps/{app}/blocks/{...}                                ← blocks
POST /bitable/v2/apps/{app}/tables/{table}/views/{view}/charts         ← v2
... （30+ 变体）
```

### SDK 源码证据
对照官方 [`larkuite/oapi-sdk-go v3_main`](https://github.com/larksuite/oapi-sdk-go/tree/v3_main/service/bitable/v1) 的源码：

- `appDashboard` 只暴露 `List / Copy` **两个动作，无 Create**
- `appTableView` 资源里**完全没有 chart 相关文件**
- 整个 SDK 没有 `chart.proto` 或对应生成代码

### 影响
**任何自动化方案都不能纯靠 API 创建图表**——必须到飞书 UI 手点。

### 变通方案
1. **手动创建**：UI 里 view → "+" → "图表" → 拖字段
2. **可执行的半自动**：先调用 API 建 view + 准备数据，再 UI 加图（5 张图人工配 ≤ 2 分钟）
3. **未来如果开放**：重跑 `utils/build_bitable_charts.py` 即可自动接管，无需改代码

---

## 限制 2：View 筛选条件 API 接收但不持久化

### 现象
```
PATCH /bitable/v1/apps/{app}/tables/{table}/views/{view_id}
Body: {"property": {"filter_info": {"conjunction": "and", "conditions": [...]}}}

→  返回 code=0（成功）
→  GET /views/{view_id} 回读 property=null
→  GET records?view_id={view_id} 仍返回全表 56 行，没过滤
```

试了多种 body 形态：
- 顶层 `filter`
- `view.filter_info`
- 带 `field_id` / `field_type` / `condition_id` 字段
- 不同的 conjunction 操作符

飞书要么静默丢弃，要么返回 `1254001 wrong body`。

### 影响
**API 能建 view 但不能设置 view 的筛选条件**——筛选必须 UI手动加。

### 变通方案
- API 仅建 view（裸 view），筛选条件在飞书 UI 加一次，后续 view_id 不变就不需要再管
- 如果要批量建多个 view with filter：暂时做不到，只能 UI

---

## 限制 3：view 的 property 结构有限

### 现象
API 返回的 view 结构里，`property` 字段只支持三类配置：

| 字段 | 支持 |
|---|---|
| `filter_info` | ⚠ API 接收但不持久化（见限制 2）|
| `hidden_fields` | ✅ 支持 |
| `hierarchy_config` | ✅ 支持 |
| `chart_config` / `charts` | ❌ **不存在** |
| `color_config` | ❌ 不存在 |
| `kanban_config` | ❌ 部分支持（看具体字段）|

### 影响
除了 filter/hidden/hierarchy，**没法用 API 控制 view 的任何 UI 配置**（颜色、图表、分组、汇总等）。

---

## 附录 A：实际工作流建议

对于 Sequoia-X 项目的 bitable 自动化：

| 任务 | 推荐做法 |
|---|---|
| 建表 + 加字段 | ✅ API 完全支持 |
| 清空 + 重写记录 | ✅ API 完全支持 |
| 建 view（裸 view） | ✅ API 支持 |
| 设置 view 筛选 | ⚠ UI 手动（API 不持久化）|
| 隐藏字段 | ✅ API 支持 |
| **建图表** | ❌ **必须 UI 手动** |
| 设置 view 颜色/分组/排序 | ❌ UI 手动 |

## 附录 B：验证用脚本

`utils/build_bitable_charts.py` 已封装成 best-effort 工具：
- view 部分：✅ 完全跑通
- chart 部分：检测到 404 自动跳过 + stderr 提示

跑一次输出会清晰标明哪些成功了、哪些需要手动 UI 完成。

## 附录 C：什么时候需要重新评估

飞书 bitable 的开放程度在快速变化。**重新评估触发条件**：
- 飞书发版日志提到 chart 相关 API
- `larkuite/oapi-sdk-go` 新增 chart 相关资源
- 第三方文章 / 社区有人报告自动化建图成功

如果发现新 API 可用，把 `utils/build_bitable_charts.py` 里 chart 部分的注释解开即可，view + 数据逻辑已经完备。