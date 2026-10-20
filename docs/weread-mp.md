# 微信读书公众号采集

`GATHER.MODEL=weread_mp` 使用微信读书采集公众号文章，保留现有 `MP_WXS_*` Feed ID 和 RSS 地址。

## 历史列表和登录凭据

历史列表使用 `GET https://i.weread.qq.com/book/articles`，参数为 `bookId`、`count=20`、`offset`、`synckey=0`，请求头使用 `accessToken`、`vid`。这是 [zlzchat 发布包](https://github.com/565800105/zlzchat/blob/292803fce88dc4efd78f81b12b837252a9ac7d4d/website.jar) 中使用的接口（`WxReadCommon.getFeedArticle`）；与原来的 Web `/web/mp/articles` 不同。

- 在“微信读书管理”扫码登录后，新保存的账号会保留原始 `accessToken`，不再仅保留 Web Cookie。
- 已保存的旧账号仍会尝试将 Cookie 的 `wr_skey` 用作 `accessToken`；重新扫码可以保存原始令牌。
- **Web 登录成功不代表 App 历史接口一定授权成功。** 若历史接口返回 401 或登录错误，需要该接口接受的有效 App 凭据，不能仅靠更换请求地址或重扫 Web 二维码保证恢复。
- 如已有同一账号的 App `accessToken`，可通过现有 Cookie 输入或 `WEREAD_COOKIE` 提供：`wr_vid=<vid>; accessToken=<App令牌>`。需要 Web 书架、Cover 或正文接口时，保留同一账号原有的 Web Cookie，在其后追加 `; accessToken=<App令牌>`。
- 不引入第三方登录服务；网页 Cookie 和 App 凭据均只请求微信读书官方域名。

配置示例：

```env
GATHER.MODEL=weread_mp
WEREAD_COOKIE=wr_vid=...; wr_skey=...; wr_rt=...
WEREAD_MP_MAX_PAGES=20
WEREAD_PAGE_INTERVAL=1
WEREAD_CONTENT_INTERVAL=2
```

需要在 RSS 中直接显示全文时，同时设置：

```env
GATHER.CONTENT=True
```

## 行为

- 新版微信读书已废弃 `/web/mp/articles` 列表接口（实测恒返回 `-2041`），前端 JS 中也不再有文章列表接口。
- 最新文章来自 `GET https://weread.qq.com/api/mp/cover?bookId=`，一次只返回该公众号**最新一篇**文章（`reviewId` + 标题 + 封面）。
- 全文来自 `GET https://weread.qq.com/web/mp/content?reviewId=`，并提取 `#js_content`。
- 采用「cover 增量」采集：每次运行取最新一篇，若该 `reviewId` 已入库则跳过；新文章拉取正文后入库并更新 Feed 同步时间。
- 正文请求失败的文章以空正文入库（RSS 仍可点击原文链接），下一轮遇到同一文章会跳过，不会重复请求。
- `-2041`、`-2012` 和 `-2010` 会明确报告为认证或风控错误，不立即重试。

## 划线 / 笔记采集（weread 模式）

- `GATHER.MODEL=weread` 时采集**书架电子书**的划线/笔记（`/web/book/bookmarklist`）。
- 公众号订阅书（`MP_WXS_*`）在微信读书内没有划线数据，划线模式下会自动跳过并提示。
- 手动「采集」接口（`/weread/collect`）会自动分档：指定 `MP_WXS_*` bookId 时走公众号文章采集，普通电子书走划线采集。

## 限制

- 拿不到历史文章列表：`/api/mp/cover` 只返回最新一篇，无法回补公众号历史文章（旧版列表接口已废弃）。
- Cookie 过期时（返回 `-2012`/`-2041`），应从浏览器重新复制 Cookie；通过环境变量提供的凭据优先于管理页保存值，管理页会标记为部署配置托管。
- 微信读书有访问频率限制，全文请求间隔建议保持 `WEREAD_CONTENT_INTERVAL >= 2`。
