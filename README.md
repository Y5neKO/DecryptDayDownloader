# DecryptDayDownloader

decrypt.day下载砸壳ipa的时候经常遇到广告拦截后获取不到下载链接的情况，类似chrome这种自带拦截的又关不掉，非常麻烦，简单写了个脚本来半自动获取下载链接

![img_6.png](img/img_6.png)

## 原理

`POST /app/{id}?/files` 是 SvelteKit form action，**app 归属由 POST body 里的 CBOR payload 决定，URL 里的 app id 只用于 Referer/路由**。

payload 是 CBOR 编码的 map，以字节十进制 CSV 形式作为 `data` 字段提交：

```json
{"appId": "<应用内部ID>", "version": "<版本名>", "isPremier": undefined}
```

几个关键点：

- `appId` 是站点数据库的内部 ID，**不是** App Store 的数字 ID（`id1467190251` 这种）。老记录是 cuid 格式（如 `cl9se40t70055dofw1xo61mwx`，2022 年批量入库），新记录是 nanoid 格式（如 `p7-UcPBfHBDiVqQ0BZ6HF`）
- 内部 ID 以明文存在于应用页面源码的 SvelteKit 序列化数据里：`app:{id:"<内部ID>",app_id:"id<数字>",...}`，版本记录则形如 `f.id="<版本ID>";f.name="<版本名>"`
- `version` 是页面上选中的版本名（如 `1.0.0`），`isPremier` 是 CBOR undefined（`0xF7`）

> 旧版脚本把抓包时的 payload 整个硬编码了，里面的 `appId` 是抓包那个应用的内部 ID，所以无论输入什么 app id，服务端都按 body 里的 appId 返回同一个应用的文件列表。

## 自动脚本

```bash
pip install requests playwright curl_cffi
python3 decrypt_day_downloader.py
```

流程：

1. 输入 app id（如 `id6754899430`）
2. playwright 驱动本机 Chrome（headful）自动过 Cloudflare，轮询拿到 `cf_clearance` 后顺带读取 UA 和页面 HTML；浏览器档案持久化在 `.cf_profile/`，cookie 有效期内重跑免验证。未装 playwright 时回退手动粘贴流程
3. 从页面提取内部 ID 和版本名（兼容 cuid / nanoid 两种格式），动态编码 CBOR payload，多组 `(appId, version)` 组合自动重试
4. 输出下载链接列表

注意：`.cf_profile/` 内含会话 Cookie，已加入 `.gitignore`，不要提交到仓库。

![img_5.png](img/img_5.png)

## 手动构造（备用）

只想手动 curl 的话按下面三步走。

### 1. 拿内部 ID 和版本名

浏览器打开应用页面，查看源码，搜 `app_id:"id<数字>"`：

- 它前面的 `id:"..."` 就是应用内部 ID（主记录）
- `x.id="...";x.name="1.0.0"` 这种闭包赋值里的 `id` 是版本 ID，`name` 是版本名

### 2. 编码 CBOR payload

把 `{"appId": 内部ID, "version": 版本名, "isPremier": undefined}` 按 CBOR 编码，每个字节转十进制逗号分隔。可以用脚本里的 `encode_cbor_payload()`，或者 `cbor2` 库自编（注意 `isPremier` 要 `0xF7` 即 undefined，不是 null）。

原始抓包示例（Chrome 应用，version 6.4.0）：

```
163,101,97,112,112,73,100,120,25,99,108,57,115,101,52,48,116,55,48,48,53,53,100,111,102,119,49,120,111,54,49,109,119,120,103,118,101,114,115,105,111,110,101,54,46,52,46,48,105,105,115,80,114,101,109,105,101,114,247
```

CBOR 解码即 `{"appId":"cl9se40t70055dofw1xo61mwx","version":"6.4.0","isPremier":undefined}`

### 3. 请求文件列表

```shell
curl -X POST "https://decrypt.day/app/id1467190251?/files" \
  -H "Origin: https://decrypt.day" \
  -H "Referer: https://decrypt.day/app/id1467190251" \
  -H "User-Agent: <你浏览器过CF时的UA，cf_clearance 与其绑定>" \
  -H 'Cookie: cf_clearance=<你的值>' \
  -F 'data=<上一步的 CSV 字节串>'
```

![img_1.png](img/img_1.png)

响应里的文件 ID 通过索引引用（`"files":4` 表示文件数组在顶层第 4 个元素）。

### 4. 拼下载链接

```
https://decrypt.day/app/id1467190251/dl/<file_id>
```

![img_2.png](img/img_2.png)

### 5. 访问下载

需要带 Referer：`https://decrypt.day/app/id1467190251`

![img_3.png](img/img_3.png)

![img_4.png](img/img_4.png)
