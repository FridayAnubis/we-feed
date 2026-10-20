# 微信读书历史抓取工作流

## 基线与原因

`fix(history)` 基于 `fix(pubdate)` 的 `6c194aff90f968e792e9c53b01bf0fd39ea72f1a`；远端没有 `fix(pubtime)`。

原代码有几处相互影响的问题：

- 把 HTTP 499、-2041、-2012 等统一显示为“所有账号配额耗尽”，覆盖上游真实原因。
- 扫码时把短 accessToken 当作可疑值，尝试用 refreshToken 或其他长字符串替换 skey，并用已不可靠的 Web 抓取接口反复验证登录。
- 未完整保存刷新令牌，无法按参考项目的移动端方式续期。
- 首次采集通常只取一页；遇到一篇已入库文章便停止，cover 时代的最新文章可能阻断历史回补。
- 任务开始前可能因宿主机 Web Cookie 刷新失败退出，新的抓取工作流根本没有执行机会。

这些是代码层面确认的问题。本地没有配置微信读书账号，不能据此断言上游当前接受某个账号或网络出口。

## 默认执行流程

`weread.history_first` 默认 `true`。对 `web/api/app/free_publish/auto/playwright` 模式，配置微信读书账号且 Feed ID / fakeid 可转换时，先执行移动端工作流；`weread_mp` 直接执行该工作流。普通笔记模式 `weread` 保持原用途。

1. **凭据**：使用原始 accessToken、skey、refreshToken、vid 和设备标识；不再按令牌长度猜测有效性。旧 Cookie 的 wr_skey / wr_rt 仍兼容。
2. **同步**：每个客户端会话先执行 mobileSync、shelf/sync、mp/list。
3. **恢复**：401 / -2010 / -2012 最多续期一次；-2041 最多补做一次会话激活；随后才切换账号。403 / 429 / 499 和相应业务限制按“账号凭据＋端点”冷却 60 秒，其他端点仍可尝试。冷却在单个服务进程内共享。
4. **列表**：首屏预取移动端 mp/cover，再请求 mp/chapters。失败后使用 article/list；两者均不可用才保留 cover 单篇兜底。cover 本身还有 Web 端回退。
5. **分页**：chapters 使用原始条目数推进 offset；article/list 使用独立 synckey，仅在 hasMore 明确为真时继续。切换来源从备用来源起点扫描并按 reviewId 去重，绝不把 offset 当 synckey。重复游标报错。
6. **元数据**：优先 originalId / doc_url / reviewId 短链；缺失元数据通过 review/single 补齐。保留 `~` 到 `_` 的链接修复。
7. **正文**：公众号原文 HTTP → 微信读书 Web 正文 → 现有浏览器抓取器。原文请求不带账号 Cookie / accessToken，恢复图片 data-src，拒绝空页面与已知错误页面。
8. **入库**：保留 Feed ID、RSS 地址、现有数据库与回调。没有发布时间时读原文，不能使用当前时间替代。部分成功后发生错误，不重放旧采集器，不推进同步时间。

首次抓取也使用 `weread.mp_max_pages`（默认 20）作为扫描上限。普通增量任务遇到整页已入库文章才停止；已入库的置顶 cover 不构成停止条件。指定非零 start_page 的历史任务不使用该提前停止条件。

公众号更新接口、手动采集接口返回 `source` / `warning`，明确区分 chapters、article/list、仅 cover。历史连接测试验证真实列表；只有 cover 可用时仍报告历史测试失败。

原采集模式仅在未配置微信读书或新工作流完全失败时接管，避免回退链再次回到同一微信读书流程。原生保活默认也走移动端请求和按需续期，旧宿主刷新不再作为任务前置条件。

## 配置与迁移

- 无新增依赖或数据库迁移。
- **建议已有用户重新扫码一次**，完整保存 accessToken、refreshToken、skey、device_id；只有旧 Cookie 且不含 wr_rt 时无法自动续期。
- 可继续使用 `GATHER.MODEL=weread_mp`，其他支持的采集模式在配置微信读书后也会优先使用新流程。
- `WEREAD_MP_MAX_PAGES=20` 控制扫描上限；保持 `WEREAD_PAGE_INTERVAL=1`、`WEREAD_CONTENT_INTERVAL=2` 或更长，避免过密请求。
- `WEREAD_HISTORY_FIRST=false` 关闭对旧采集模式的优先包装及移动端保活；显式 `weread_mp` 仍使用新的历史实现。
- 环境变量 Cookie 优先，绝不混入磁盘其他账号凭据。环境管理的凭据续期仅在当前客户端生效，不覆盖部署配置。
- 沿用现有代理设置；没有照搬参考项目关闭系统代理、读取其他应用账号文件的行为。
- 刷新写回只合并对应账号；若期间重新扫码或替换 Cookie，较新的配置优先。持久化失败会记录警告，本次请求仍可使用已续期令牌。

## 验证与边界

相关测试覆盖签名固定向量、短令牌扫码、完整凭据保存、401 续期、499 业务码、同步步骤、失败重试上限、端点冷却、账号切换、凭据合并、列表来源切换、分页游标、置顶去重、历史回补、正文回退、失败后同步状态和完成回调。

2026-10-21：上述相关模块共 75 项测试通过，测试期间拦截未模拟的 HTTP 请求。使用独立临时配置启动 `main.py` 成功，OpenAPI 包含历史测试接口，该接口未登录访问返回 401；未对真实账号执行抓取。

```sh
python -m unittest core.wx.model.test_weread_mp core.wx.model.test_weread_history apis.test_weread tests.test_weread_client tests.test_weread_workflow tests.test_article_refresh_task tests.test_200013_fallback
```

测试使用模拟微信响应，不代表真实账号验证。历史范围由微信读书提供的数据决定；article/list 未返回 hasMore 时按一次快照处理，不推断它包含公众号全部历史。验证码/上游访问限制仍可能需要用户在官方客户端处理。刷新协议和接口均为非公开 API，可能变化。

## 参考实现

固定参考 [wechat-mp-tools](https://github.com/x554960766/wechat-mp-tools) 提交 `5c0a6b7a3740f1e28087e2cc8577c3059e5a17f3`：

- [backend/articles.py](https://github.com/x554960766/wechat-mp-tools/blob/5c0a6b7a3740f1e28087e2cc8577c3059e5a17f3/backend/articles.py)：cover、chapters、article/list、review/single。
- [backend/account_pool.py](https://github.com/x554960766/wechat-mp-tools/blob/5c0a6b7a3740f1e28087e2cc8577c3059e5a17f3/backend/account_pool.py)：账号轮换、认证头、续期与冷却。
- [backend/weread_signer.py](https://github.com/x554960766/wechat-mp-tools/blob/5c0a6b7a3740f1e28087e2cc8577c3059e5a17f3/backend/weread_signer.py)：签名及 mobileSync 协议。签名代码保留 MIT 声明。
- [backend/downloader.py](https://github.com/x554960766/wechat-mp-tools/blob/5c0a6b7a3740f1e28087e2cc8577c3059e5a17f3/backend/downloader.py)：原文、WeRead、浏览器的正文获取顺序。

后续可继续评估 `/mp/list`、`/subscription/books` 与书架聚合，补齐管理页的公众号来源；以及显式展示需要人工验证的状态。这些需要真实账号响应验证，本次没有照搬未验证的滑块或跨应用登录数据导入流程。

## 参考代码许可证

`mobileSync` 协议字段适配自上述 MIT 项目，保留其许可声明：

```text
MIT License

Copyright (c) 2026 xuyi

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
