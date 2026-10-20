# 微信读书公众号采集

配置微信读书账号后，默认优先使用参考 `wechat-mp-tools` 的移动端工作流。直接请求 `weread.qq.com` / `i.weread.qq.com`，保留已有 Feed ID 和 RSS 地址。

## 配置

1. 在管理页扫码授权。已有用户建议重新扫码一次，以完整保存 `accessToken`、`refreshToken`、`skey` 和设备标识。
2. 运行“公众号采集连接测试”，它验证实际历史列表，而不是仅验证最新一篇 cover。
3. 执行公众号更新或手动采集。

无需新增必填配置。可显式指定：

```env
GATHER.MODEL=weread_mp
WEREAD_MP_MAX_PAGES=20
WEREAD_PAGE_INTERVAL=1
WEREAD_CONTENT_INTERVAL=2
```

需要全文时设置 `GATHER.CONTENT=True`。旧 Cookie 配置仍兼容：

```env
WEREAD_COOKIE=wr_vid=...; wr_skey=...; wr_rt=...
```

环境变量凭据优先于管理页账号。没有刷新令牌的旧 Cookie 无法自动续期；不会把 refreshToken 当成 accessToken 或 skey。

## 抓取流程

- 按需激活移动会话：`mobileSync → shelf/sync → mp/list`。
- 认证失败时尝试一次令牌续期；`-2041` 时最多补做一次激活，再轮换账号。请求限制按账号与端点冷却 60 秒，不统一显示为“配额耗尽”。
- 预取最新 cover，优先 `/mp/chapters`，失败时转 `/article/list`，最后才降级为仅取 cover。返回的 `source` / `warning` 标明来源与降级情况。
- 首次抓取与增量补抓均可扫描至 `WEREAD_MP_MAX_PAGES` 上限；按原始条目数或备用接口 synckey 翻页。reviewId 去重；遇到一篇已入库文章不会立即阻断历史回补。
- 元数据缺失时用 `/review/single` 补齐；正文依次使用原文 HTTP、WeRead Web、浏览器。
- 发布时间缺失时读取原文；没有可靠时间就报告失败，不以抓取时间代替。
- 翻页中途或正文失败不会推进 Feed 同步时间；下轮按数据库记录继续补抓。

默认 `WEREAD_HISTORY_FIRST=true`：在 `web/api/app/free_publish/auto/playwright` 模式下，有微信读书凭据且账号标识可转换时先尝试上述流程；完全失败再回退原采集器。原生保活也优先使用移动端按需续期，不再以宿主机 Web Cookie 刷新作为定时采集的前置条件。

设置 `WEREAD_HISTORY_FIRST=false` 可关闭上述优先策略；显式选择 `weread_mp` 仍会使用新历史流程。

## 笔记与限制

- `GATHER.MODEL=weread` 仍采集普通电子书的笔记/划线；公众号文章使用 `weread_mp`。
- 上游决定可获取的历史范围，不能保证包含公众号全部历史；cover 降级只获取最新一篇。
- 冷却状态在单个服务进程内共享。验证码等限制可能仍需在官方客户端处理。
- 当前开发环境没有配置账号，验证以模拟响应、接口测试和服务启动检查为主，真实抓取效果需要部署后确认。

完整实现依据和测试说明见 [历史抓取工作流](history-fetch.md)。
