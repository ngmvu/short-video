# Short Video Distributor

Python 3.10+ workflow for generating short-video ideas with Gemini, approving them
in Telegram, producing videos in Google Flow, publishing through the official
YouTube/TikTok/Facebook APIs, and sending a Telegram report.

## Cấu trúc mã nguồn

- `main.py`: điểm vào CLI và điều phối workflow.
- `core/tele/`: bot Telegram, polling listener và gửi thông báo/video.
- `core/gemini/`: tạo ý tưởng, gợi ý chủ đề và định dạng prompt Gemini.
- `core/`: cấu hình, lưu trữ, phân tích kịch bản, Google Flow và đăng mạng xã hội.
- `web_app.py`: dashboard Flask; khởi chạy listener Telegram khi dashboard chạy.

## Cấu hình

### 1. Cài đặt

Tạo virtualenv một lần:

```powershell
py -3.10 -m venv .venv
```

Sau đó chọn lệnh theo terminal bạn đang sử dụng.

**PowerShell** — dấu nhắc thường bắt đầu bằng `PS`:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

**Command Prompt (CMD)** — dấu nhắc thường có dạng `E:\duong-dan>`:

```bat
.\.venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
```

Sau khi kích hoạt thành công, dấu nhắc sẽ có tiền tố `(.venv)`. Không chạy
`Activate.ps1` trực tiếp từ CMD.

Mở file `.env` vừa tạo và điền cấu hình bên dưới. Giữ nguyên dấu nháy nếu giá trị
có ký tự đặc biệt; không commit `.env`, `auth.json`, `yt_token.json` hoặc OAuth
client secret lên Git.

### 2. Telegram — bắt buộc

1. Mở Telegram, nhắn `/newbot` cho **@BotFather**, tạo bot và copy token vào
   `TELEGRAM_BOT_TOKEN`.
2. Mở bot mới tạo và nhấn **Start** để bot có thể nhắn cho bạn.
3. Lấy chat ID cá nhân bằng cách thay `<BOT_TOKEN>` bằng token của bot trong URL
   sau, mở URL trên trình duyệt, rồi đọc trường `message.chat.id`:

   ```text
   https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
   ```

   Điền ID số đó vào `TELEGRAM_CHAT_ID`. Nếu `result` đang rỗng, hãy nhắn bot
   một tin rồi tải lại URL. Không chia sẻ URL này vì token nằm trong đó.

### 3. Gemini — bắt buộc để tạo ý tưởng

- Tạo API key trong Google AI Studio và đặt vào `GEMINI_API_KEY`.
- Để tự chuyển key khi key hiện tại hết quota/gặp lỗi, đặt các key dự phòng vào
  `GEMINI_FALLBACK_API_KEYS`, phân tách bằng dấu phẩy hoặc chấm phẩy. Ví dụ:
  `GEMINI_FALLBACK_API_KEYS=key2,key3`. Tính năng này áp dụng cho các yêu cầu
  Gemini trong ứng dụng; log sẽ báo khi chuyển key. Không ghi key thật vào tài liệu
  hoặc chia sẻ log/chụp màn hình có chứa key.
- `GEMINI_MODEL` mặc định là `gemini-3.8-flash`; model khả dụng có thể phụ thuộc
  project/tài khoản và thay đổi theo thời gian.
- `/choice` cho phép chọn nhiều dạng nội dung cho bộ đôi khỉ chủ tiệm sửa xe và
  ông chú: khịa thói hóng phốt/xin link dính bẫy, hỏi xoáy đáp xoay, cà khịa,
  chuyện tiệm xe và đời thường; có thể nhập tình huống riêng để Gemini giữ mấu
  chốt hài rồi phát triển thành kịch bản, hoặc nhờ Gemini nghĩ thêm chủ đề.
- `IDEA_STYLE` điều khiển phong cách sáng tác. Chủ đề đã chọn được ưu tiên khi
  Gemini tạo các biến thể kịch bản; có thể đổi `IDEA_STYLE` trong mục Gemini
  trên dashboard.
- Dự án dùng SDK mới `google-genai`. Nếu đổi model, chọn model được hỗ trợ bởi
  Gemini API và có khả năng sinh JSON.

### 4. Google Flow — bắt buộc để render video

1. Đăng nhập và mở project Flow muốn dùng. Sao chép URL project vào
   `FLOW_PROJECT_URL`.
2. Tạo Playwright storage state:

   ```powershell
   python scripts\save_flow_auth.py
   ```

   Khi chạy trong terminal tương tác, chọn **1** để tiếp tục dùng tài khoản hiện
   tại hoặc **2** để đổi tài khoản. Nếu chọn đổi, chuyển tài khoản trong cửa sổ
   Chrome do script mở, vào đúng project Flow, kiểm tra avatar tài khoản rồi nhấn
   Enter để lưu. Có thể chọn trực tiếp bằng `--account-mode keep` (giữ) hoặc
   `--account-mode switch` (đổi). Mặc định file là `auth.json`; có thể đổi bằng
   `FLOW_AUTH_FILE`. Đăng nhập trong tab trình duyệt VS Code khác không tự chia sẻ
   cookie sang cửa sổ này. Các thao tác từ Dashboard/Telegram vẫn tự động dùng
   phiên tài khoản hiện có.
3. Flow không có UI automation contract ổn định. Dashboard dùng các CSS selector
   đã xác minh trên UI Flow hiện tại; giao diện có thể thay đổi. Workflow đặt
   Video, 9:16 và một output, tạo một clip từ prompt kết hợp các scene, rồi tải
   **720p Original size**. Thời lượng/cảnh có thể nhập trong dashboard; thời lượng
   cần khớp một tùy chọn thật sự có trong Flow:

   | Biến | Selector cần trỏ tới |
   | --- | --- |
   | `FLOW_AGENT_TOGGLE_SELECTOR` | Tắt chế độ Agent để không chờ xác nhận tương tác |
   | `FLOW_SETTINGS_SELECTOR` | Nút mở cài đặt generation trong chế độ prompt trực tiếp |
   | `FLOW_VIDEO_MODE_SELECTOR` | Chọn chế độ Video |
   | `FLOW_ASPECT_RATIO_SELECTOR` | Chọn khung dọc 9:16 |
   | `FLOW_DURATION_SELECTOR` | Selector thời lượng; dùng `{seconds}` để thay theo thời lượng yêu cầu |
   | `FLOW_OUTPUT_COUNT_SELECTOR` | Chọn một output để tránh tạo clip trùng |
   | `FLOW_PROMPT_SELECTOR` | Ô prompt của Flow |
   | `FLOW_SUBMIT_SELECTOR` | Nút bắt đầu generation |
   | `FLOW_RESULT_SELECTOR` | Thẻ video mới; phải xuất hiện thêm thẻ sau khi render |
   | `FLOW_DOWNLOAD_MENU_SELECTOR` | Nút More options trong trang chi tiết media |
   | `FLOW_DOWNLOAD_SELECTOR` | Mục 720p Original size trong menu Download media |

   Selector mặc định được xác minh trực tiếp trên Flow UI ngày 2026-10-31 và
   được điền vào `.env` hiện tại. Chúng không phải cam kết API ổn định; kiểm tra
   lại bằng DevTools (**Inspect**) nếu Google thay đổi giao diện. Script lưu
   `auth.json` sẽ kiểm tra prompt editor đã mở trước khi ghi file; đăng nhập ở
   cửa sổ Chromium do script mở, không phải một tab trình duyệt khác.
   `FLOW_GENERATION_MODE=agent` gửi toàn bộ cảnh trong một prompt đến Flow Agent
   cho cả kịch bản tạo tự động lẫn prompt gửi trực tiếp từ Telegram; Agent vẫn
   tạo từng cảnh thành clip riêng. Đặt `FLOW_GENERATION_MODE=direct` nếu muốn
   quay lại cách gửi từng cảnh lần lượt qua ô Prompt.
4. `FLOW_RENDER_TIMEOUT_MS` và `FLOW_DOWNLOAD_TIMEOUT_MS` là timeout mili-giây,
   mặc định 900000 (15 phút). Kết quả MP4 được lưu dưới `storage/downloads/`.

### 5. YouTube Shorts — cấu hình nếu muốn đăng YouTube

1. Trong Google Cloud Console, tạo project, bật **YouTube Data API v3**, cấu hình
   OAuth consent screen và tạo OAuth Client ID loại **Desktop app**.
2. Tải file client JSON về workspace dưới tên `client_secret.json`, hoặc đặt
   đường dẫn khác tại `YOUTUBE_CLIENT_SECRETS_FILE`.
3. Tạo token bằng lệnh:

   ```powershell
   python scripts\authorize_youtube.py
   ```

   Đăng nhập đúng tài khoản/kênh YouTube và chấp thuận quyền upload. Mặc định
   token nằm ở `yt_token.json`; đổi đường dẫn qua `YOUTUBE_TOKEN_FILE`. Cả hai
   file OAuth đều là thông tin nhạy cảm.

### 6. TikTok — cấu hình nếu muốn đăng TikTok

- Đăng ký ứng dụng trong TikTok for Developers, yêu cầu quyền **Content Posting
  API** phù hợp, hoàn thành OAuth theo yêu cầu của TikTok và lấy access token.
- Đặt token vào `TIKTOK_ACCESS_TOKEN`. Token có thể hết hạn; cần thực hiện lại
  quy trình cấp/refresh token của ứng dụng TikTok khi API trả lỗi xác thực.
- `TIKTOK_PRIVACY_LEVEL` mặc định `PUBLIC_TO_EVERYONE`. Giá trị khả dụng phụ
  thuộc quyền và trạng thái tài khoản/app; chỉ dùng giá trị TikTok cho phép.
- API có thể trả publish ID khi video còn đang xử lý/kiểm duyệt. Khi đó báo cáo
  Telegram ghi ID thay vì link bài đăng.

### 7. Facebook Reels — cấu hình nếu muốn đăng Reels

- Tạo Meta app và cấu hình quyền/đăng nhập cho Page theo yêu cầu của Meta Graph
  API và ứng dụng của bạn.
- Điền Page ID vào `FB_PAGE_ID`, Page access token có quyền đăng video vào
  `FB_PAGE_ACCESS_TOKEN`. Không dùng user token thiếu quyền Page publishing.
- API version hiện đặt trong code là `v19.0`; hãy xác minh phiên bản còn được
  Meta hỗ trợ và quyền ứng dụng trước khi chạy thật.

### 8. Các biến còn lại

| Biến | Ý nghĩa |
| --- | --- |
| `LOG_LEVEL` | Mức log Python, ví dụ `INFO`, `WARNING`, `DEBUG` |
| `FLOW_AUTH_FILE` | Đường dẫn storage state của Playwright |
| `YOUTUBE_CLIENT_SECRETS_FILE` | Đường dẫn OAuth client JSON |
| `YOUTUBE_TOKEN_FILE` | Đường dẫn token YouTube đã cấp quyền |

Đường dẫn tương đối được tính từ thư mục dự án. Trước khi đăng công khai, hãy
kiểm thử riêng từng nền tảng và xác nhận quyền, quyền riêng tư, caption cũng như
nội dung video. Chỉ cần bật và cấu hình các nền tảng bạn định sử dụng; platform
thiếu cấu hình sẽ được ghi nhận là lỗi riêng trong báo cáo.

## Chạy hệ thống

### Bảng điều khiển Flask

Tạo môi trường ảo và cài các thư viện của dự án (chỉ cần làm một lần):

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Nếu dùng Command Prompt (CMD), thay dòng kích hoạt PowerShell bằng
`.\.venv\Scripts\activate.bat`.

Sau đó chạy giao diện:

```powershell
.\.venv\Scripts\Activate.ps1
python web_app.py
```
Lệnh này tự khởi động listener Telegram cùng dashboard và dừng listener do nó
tạo khi dashboard thoát. Không cần chạy thêm `python main.py --telegram-poll`.
Các workflow khởi động từ dashboard dùng listener chung này, tránh xung đột
`getUpdates`.

Trong CMD, dùng:

```bat
.\.venv\Scripts\activate.bat
python web_app.py
```

Hoặc chạy nhanh bằng script:

```powershell
.\run_dashboard.ps1
```

Mở <http://127.0.0.1:5000>. Sidebar chia dashboard thành các khu vực workflow,
vận hành, nội dung, video, lịch sử và log. Cấu hình được chia theo Gemini,
Telegram, Google Flow, từng nền tảng đăng và ứng dụng; lưu một nhóm không làm
thay đổi các nhóm cấu hình khác. Giao diện được tách thành các template,
stylesheet và JavaScript riêng. Trình thiết kế workflow cho phép kéo-thả node, nối các
bước, lưu bố cục trên trình duyệt và chạy node riêng hoặc cả flow theo thứ tự
topological của các kết nối. Flow mẫu tạo ý tưởng rồi chạy node
**Duyệt → video → Telegram**: bot chờ duyệt, tạo video sau khi duyệt và gửi video
trở lại Telegram, không tự đăng mạng xã hội. Node lắng nghe/duyệt phải ở cuối flow;
nút dừng trên dashboard dừng listener. Node **Sản xuất & đăng** vẫn đăng lên các
nền tảng đã cấu hình. Secret không được hiển thị lại; để trống ô secret khi lưu
để giữ giá trị hiện tại.

Mục **Tạo ý tưởng từ prompt riêng** nhận prompt tiếng Việt trực tiếp, số cảnh
(1–20) và tổng thời lượng (1–180 giây). Mỗi ý tưởng được gửi sang Telegram để
duyệt như trước. `FLOW_DURATION_SELECTOR` mặc định thay `10s` trong selector
hiện tại bằng thời lượng đã nhập; có thể dùng selector mẫu
`button[role="radio"]:has-text("{seconds}s")`. Giá trị đó phải khớp một lựa chọn
được Flow hỗ trợ, nếu không bước tạo video sẽ báo lỗi thay vì âm thầm chọn sai.
Khi dùng tab Agent, hệ thống gửi tất cả Scene trong một prompt kèm số lượng clip
cần tạo. Nếu Flow tạo đủ cùng lúc, hệ thống tải tất cả; nếu Agent dừng sau một
cảnh, hệ thống tự duyệt yêu cầu tiếp theo và yêu cầu tạo các cảnh còn thiếu.
Dashboard chỉ bind vào `127.0.0.1`, kiểm tra CSRF, và không nên expose ra LAN hay
Internet. Dashboard dùng Flask development server để chạy local; không dùng
server này như dịch vụ public. Trạng thái Flow ở đây chỉ xác nhận file session,
project URL và selectors; nó không thể kiểm tra render đang chạy qua API.
Chỉ chạy một tiến trình lắng nghe Telegram cho mỗi bot; ứng dụng dùng file lock
để từ chối listener thứ hai thay vì gây xung đột `getUpdates`.

Trên Telegram, gửi `/start` hoặc `/menu` để mở bảng điều khiển:
nhập chủ đề rồi chọn tạo 3, 5 hoặc 10 ý tưởng bằng Gemini; dán kịch bản có sẵn
để tạo video không qua Gemini; dán prompt Flow nhiều Scene qua chế độ trực tiếp
để giữ nguyên prompt và không gọi Gemini; hoặc xem hàng đợi render. Bot cần chạy
listener Telegram để nhận lệnh và xử lý các nút tương tác.
Khi listener khởi động, bot thay danh sách lệnh Telegram (cả mặc định và riêng
cho chat đã cấu hình) bằng các lối tắt: `/choice` để bắt đầu tạo ý tưởng bằng
Gemini, `/new` để dán kịch bản có sẵn, `/direct` để dán prompt Google Flow dạng
`Scene — Input / First Frame: @khi @nguoi ...` mà không qua Gemini, và `/hang_doi`
để xem hàng đợi. Trong danh sách, chọn **Chạy trước** để đưa một kịch bản lên đầu;
worker sẽ chạy mục đó trước rồi tiếp tục các mục còn lại. Mỗi Scene đặt trên một
dòng; có thể đánh số hoặc để bot tự đánh số, không cần ghi thời lượng.
Sau khi gửi `/direct`, dán prompt ở tin nhắn kế tiếp; lệnh cũng nhận prompt ngay
sau `/direct`. Prompt được đưa vào hàng duyệt và không hiện nút Gemini chỉnh sửa.
Prompt `/direct` giữ nguyên nội dung từng Scene và được gửi vào khung tạo video
thường, không qua tab Agent để tránh Agent diễn giải hoặc viết lại prompt.
Định dạng `Scene N — Input / First Frame: @nhanvat: ...` được hỗ trợ ngay cả
khi phần mô tả cảnh nằm trên cùng dòng. Sau khi nhập prompt Scene, bot gắn các
Character asset có tag trong `First Frame` rồi mới gửi yêu cầu tạo video. Với
thời lượng `auto`, thời lượng clip được ước tính từ toàn bộ câu thoại đặt trong
ngoặc kép; thời lượng khai báo cũng được tăng nếu ngắn hơn ước tính để giảm nguy
cơ thoại bị cắt.
Các lệnh `/start` và `/menu` vẫn mở bảng điều khiển; `/idea`, `/queue` và
`/cancel` vẫn dùng được nếu nhập trực tiếp.
Khi dùng `/choice`, bot phản hồi ngay bằng trạng thái đang lấy chủ đề rồi cập
nhật tin nhắn bằng các lựa chọn Gemini; nếu Gemini lỗi, bot sẽ báo lỗi và ghi
chi tiết ra terminal đang chạy listener.

Nếu PowerShell chặn chạy script kích hoạt, chỉ cho phép script trong terminal
hiện tại rồi kích hoạt lại:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

```powershell
# Generate ideas, send Telegram buttons, and keep the callback listener active
python main.py --generate-ideas

# Run only the Telegram approval listener (useful alongside a scheduled process)
python main.py --telegram-poll

# Produce approved scripts, publish to all configured platforms, report to Telegram
python main.py --process-queue

# Keep Telegram polling active; generate ideas every 6 hours and check the queue
# every minute. Override the idea cadence with --interval-minutes.
python main.py --auto --interval-minutes 360
```

An approval callback appends an item to `storage/queue_approved.json`. Successfully
rendered items are removed before platform publishing; each platform's publishing
result (including API errors) is reported independently. TikTok may return a
publish ID before its moderation/processing is complete, rather than a public
post URL. Keep the process running for Telegram callbacks and the `--auto`
scheduler, or run the listener and processing command under your process manager.
#   s h o r t - v i d e o  
 