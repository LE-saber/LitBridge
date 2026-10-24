# LitBridge

[English](https://github.com/LE-saber/LitBridge/blob/main/README.md) | [简体中文](https://github.com/LE-saber/LitBridge/blob/main/README.zh-CN.md) | **繁體中文** | [日本語](https://github.com/LE-saber/LitBridge/blob/main/README.ja.md)

**為 AI agent 提供以本機為優先的文獻工作流程與文件正規化服務。**

LitBridge 保留檢索結果、來源與原始文件，管理可恢復的擷取工作，將文件轉換為附有原頁或節點定位的 Markdown，再透過 CLI 或 MCP 提供閱讀。所有網路來源均透過開放的 Provider 協定接入。核心不附帶文獻網站實作、網站目錄、登入步驟配方或來源憑證。

| 專案資訊 | 目前基準 |
| --- | --- |
| 應用程式 / 通用用戶端 | 0.2.1 |
| Provider 協定 | 1.0 |
| 執行環境 | Python 3.11+ |
| 介面 | CLI 與透過 stdio 提供的 16 個 MCP 工具 |
| 預設來源 / 雲端處理 | 未啟用任何 Provider；雲端處理關閉 |
| 發布方式 | 本儲存庫 Releases 或由原始碼安裝 |

## 功能

- Provider 協定 1.0，具備能力宣告與明確啟用機制；支援多來源結果合併、DOI／來源識別、快取、逾時隔離及熔斷。
- 持久工作佇列、租約、失敗分類、人工協助恢復及既有原件重用；分別回報擷取、格式驗證、正規化與閱讀結果。
- 有大小上限的 PDF/XML 原件儲存與 SHA-256 驗證；產生附原頁／節點定位的 Markdown 與結構化區塊。也可正規化明確匯入的本機 HTML。
- 可選的離線版面分析／OCR、逐頁檢查點與取消後恢復；可選的雲端公式辨識及可設定的影像模型比較。雲端處理預設關閉。
- CLI 與 16 個 MCP 工具共用服務層。通用瀏覽器工具僅操作 Provider 提供的受控目標，不包含網站規則。

## 安裝

需要 Python 3.11+。從 [Releases](https://github.com/LE-saber/LitBridge/releases) 下載 wheel，或在原始碼目錄安裝。以下命令適用於 Windows PowerShell：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[mcp]"
Copy-Item examples/litbridge.toml litbridge.local.toml
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml providers
```

安裝並啟用 Provider 前，來源清單為空。若要試用開放協定的中立本機目錄範例：

```powershell
.venv/Scripts/python.exe -m pip install --no-deps -e examples/localcatalog
```

將自己有權使用的中繼資料存成 JSON 陣列，例如 `[{"title":"Synthetic study","doi":"10.5555/example","year":2024}]`，再於本機設定加入：

```toml
enabled_plugins = ["localcatalog"]
[plugin_options.localcatalog]
path = "D:/YOUR_PATH/catalog.json"
```

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml search "Synthetic" --provider localcatalog
```

本機目錄範例只提供中繼資料。範例 DOI 不保證在網路上存在，也不提供全文。其他來源須獨立安裝，並將其 ID 明確加入 `enabled_plugins`；核心不依賴其儲存庫或實作。

## 使用與 MCP

```powershell
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml doctor
.venv/Scripts/python.exe -m litbridge --config litbridge.local.toml mcp
```

MCP 使用 stdio，請參考 [設定範例](examples/mcp.json)。工具包含 `providers`、`search`、`resolve`、`access`、`retrieve`、`references`、`import_url`、`doctor`、`batch`、`job_create`、`job_run`、`job_status`、`job_history`、`human_run`、`normalize` 與 `read`。先檢查來源能力，再篩選並擷取少量目標；保留原件後，分別確認正規化與閱讀結果。完整步驟見 [閱讀工作流程](docs/READING_WORKFLOW.md)。

Release 資產 `litbridge-core-plugin.zip` 是通用 Codex/MCP 用戶端封裝，內含核心服務，但不含文獻網站實作。解壓縮後依套件說明安裝相依套件，並提供本機設定。wheel 適合既有 Python 環境；原始碼 ZIP 適合開發。本版本未發布至 PyPI，請勿假定 `pip install litbridge` 會取得它。

## 正規化與模型

輕量解析不需要大型模型。複雜版面可使用獨立的離線執行環境。掃描頁面、雙欄閱讀順序、表格與公式可能需要複核；`ready` 狀態與處理進度不代表準確率。需要未知開啟密碼的文件會略過，原始文件仍保留。

影像模型須透過明確的本機設定接入，憑證保留在程序環境或忽略追蹤的檔案中。預設要求 HTTPS。只有明確啟用時，才將選定的公式裁切影像送往外部；模型間的一致程度不等於準確率。詳見 [模型設定](docs/MODEL_SERVICES.md) 與 [正規化](docs/NORMALIZATION.md)。

## 安全與邊界

文獻、網頁與模型輸出均視為不可信資料。Provider 是使用者信任的 Python 套件，目前未以程序沙箱隔離。核心不提供訂閱權限、不解決驗證碼，也不代為登入。API 權限、瀏覽器權限、擷取成功與可讀性須分別驗證。核心網路工具限制 HTTPS、目標網域、大小及重新導向時的憑證處理。

不要提交金鑰、機構工作階段、瀏覽器設定檔、下載的論文、全文、模型權重或本機報告。雲端增強可能傳送選定影像並產生費用，應先確認設定與限額。詳見 [安全與隱私](docs/SECURITY.md)。

## 開發

```powershell
.venv/Scripts/python.exe -m pip install -e ".[dev,mcp,browser]"
.venv/Scripts/python.exe -m pytest -m "not browser and not live" -q
.venv/Scripts/python.exe scripts/update_manifest.py
.venv/Scripts/python.exe scripts/check_manifest.py
.venv/Scripts/python.exe scripts/build_plugin.py
```

公開 API：[Provider 協定](docs/PROVIDER_PROTOCOL.md)。版本資訊：[變更紀錄](CHANGELOG.md)。CI 在獨立環境驗證核心、官方 MCP SDK 與發布套件，不存取機構工作階段。合成測試不能取代網路／瀏覽器驗收。

目前版本為 0.2.1，Provider 協定為 1.0。舊設定中屬於特定來源的欄位應移至對應獨立 Provider 的選項；核心預設沒有來源。

## 授權條款

本儲存庫全部專案程式碼與文件採用 [MIT 授權條款](LICENSE)，包含核心、開放 Provider 協定、CLI/MCP、通用用戶端封裝、正規化／模型介面、範例與測試。獨立安裝的 Provider 套件適用其自身授權，本儲存庫的授權不替它們授予權利。第三方相依套件與模型權重保留各自授權；原始文獻的著作權仍歸權利人所有。
