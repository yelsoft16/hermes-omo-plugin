# Hermes–OmO RPC Plugin — Tasarım Spesifikasyonu

- **Tarih:** 2026-10-19
- **Durum:** Tasarım onaylandı; uygulama planı bekliyor
- **Kapsam:** Hermes Agent için yerel plugin (`~/.hermes/plugins/omo/`), v1
- **OmO sürümü:** 5.1.35 (engine senpi 2026.10.16), host RPC protokolü v1

---

## 1. Amaç

Hermes'in Telegram/CLI üzerinden OmO (oh-my-openagent) oturumlarını başlatmasını,
OmO bir soru sorduğunda bunu kullanıcıya iletmesini, cevabı aynı oturuma geri
göndermesini ve işi sonuna kadar sürdürmesini sağlamak.

v1'in birincil kullanımı: `/ulw-plan` gibi interaktif planlama akışlarını uçtan uca
çalıştırmak. OmO planlama sırasında `ask_user_question` / `request_user_input`
araçlarıyla soru sorar; bu sorular Hermes'in native `clarify` aracıyla kullanıcıya
ulaşır ve cevaplar OmO'ya döner.

### Neden RPC?

- `omo -p` (headless) modunda soru aracı çalışmaz; model "This session has no user
  attached (subagent or headless); decide on best judgment" alır.
- `omo -p` + `-c` yalnızca turlar arası düz metin soru-cevap sağlar; mid-turn değil.
- `omo --mode rpc` / host soketi, `extension_ui_request` → `extension_ui_response`
  ile gerçek mid-turn soru-cevap verir.
- Host modunda bekleyen sorular bağlantı kopsa bile `pendingQuestions` ile replay
  edilir; oturum Hermes/gateway restart'ına dayanır.

---

## 2. Karar kaydı

| # | Karar | Gerekçe |
|---|-------|---------|
| D1 | v1 kapsamı: `/ulw-plan` uçtan uca | En zor akışı kanıtlar; tool yüzeyi genel kalır |
| D2 | Taşıma: `omo host` daemon + attach | Kalıcılık, replay; spike ile doğrulandı |
| D3 | Varsayılan permission: `full-access` | Kullanıcı tercihi; her çağrıda override edilebilir |
| D4 | `cwd` opsiyonel; varsayılan `/home/frappe/frappe-bench` | Telegram'da cwd güvenilir değil |
| D5 | Her `omo_start` **yeni** oturum açar | Kullanıcı net talebi; otomatik devam yok |
| D6 | Biten oturum otomatik kapanır | Bellek temizliği; transkript diskte kalır |
| D7 | Resume yalnızca açık çağrıyla (`omo_resume` / `/omo resume`) | Sürpriz bağlam taşınmasını önler |
| D8 | Aynı Hermes oturumunda çalışan oturum varken `omo_start` reddeder | Kafa karışıklığını önler; `parallel=true` ile aşılabilir |
| D9 | Plugin, tool çağrıları arasında süreç tutmaz | Host oturumu tutar; plugin stateless kalır |
| D10 | Spec ve kod önce yerel plugin, sonra git repo | Hızlı doğrulama; taşınabilirlik sonra |

---

## 3. Mimari

```
Telegram / CLI
      │
      ▼
Hermes modeli
      │  omo_start / omo_wait / omo_answer / omo_status / omo_cancel / omo_resume
      ▼
┌─────────────────────────────────────────────────────┐
│ omo plugin  (~/.hermes/plugins/omo/)                │
│                                                     │
│  tools.py        tool şemaları + handler'lar        │
│  host_client.py  Unix socket, JSONL, D1 protokolü   │
│  sessions.py     Hermes session_id → omo kaydı      │
│  questions.py    frame normalizasyonu               │
│  skills/omo-orchestration/SKILL.md                  │
└─────────────────────────────────────────────────────┘
      │  Unix socket: ~/.omo/agent/rpc/rpc.sock
      ▼
┌─────────────────────────────────────────────────────┐
│ omo host daemon  (omo host ensure)                  │
│  - çoklu oturum, retain_on_disconnect               │
│  - bekleyen soruları saklar ve replay eder          │
└─────────────────────────────────────────────────────┘
      │
      ▼
┌─────────────────────────────────────────────────────┐
│ omo session                                         │
│  - cwd, model, permission preset, transcript        │
│  - `/ulw-plan`, `/ulw-execute`, `ultrawork`, ...    │
└─────────────────────────────────────────────────────┘
```

### Bileşen sınırları

- **`host_client.py`** Hermes'i bilmez. Yalnızca soket bağlantısı, JSONL framing,
  protokol komutları ve hata tipleriyle ilgilenir.
- **`sessions.py`** RPC ayrıntısını bilmez. Kalıcı kayıtları ve durum geçişlerini
  yönetir.
- **`questions.py`** frame'leri tek bir `QuestionPayload` yapısına çevirir; cevap
  map'lemesini yapar.
- **`tools.py`** yalnızca bu modülleri çağırır; Telegram/gateway bilgisi yoktur.
- **Skill** modelin akışı doğru sırayla çağırmasını sağlar; iş mantığı içermez.

---

## 4. Dosya yapısı

```
~/.hermes/plugins/omo/
├── plugin.yaml
├── __init__.py          # register(ctx)
├── tools.py             # 6 tool + handler'lar
├── host_client.py       # socket + JSONL + protokol
├── sessions.py          # SQLite state
├── questions.py         # normalizasyon + cevap map'leme
├── schemas.py           # tool JSON şemaları
├── skills/
│   └── omo-orchestration/
│       └── SKILL.md
├── tests/
│   ├── test_framing.py
│   ├── test_questions.py
│   ├── test_sessions.py
│   └── test_host_integration.py
└── docs/
    └── specs/
        └── 2026-10-19-omo-plugin-design.md
```

`plugin.yaml`:

```yaml
name: omo
version: 0.1.0
description: "OmO (oh-my-openagent) RPC bridge: interactive omo sessions from Hermes."
author: Selçuk ÇALIŞKAN
requires_hermes: ">=0.21.5"
provides_tools:
  - omo_start
  - omo_wait
  - omo_answer
  - omo_status
  - omo_cancel
  - omo_resume
provides_hooks: []
```

Ayrıcalıklı capability yoktur (`tools.override` vb. kullanılmaz).

---

## 5. Host protokolü — doğrulanmış akış

Kaynak: `~/.omo/binary-runtime/5.1.35/docs/rpc.md` ve 2026-10-19 spike'ı.

### 5.1 Bağlantı ve kimlik

Her tool çağrısı yeni bir Unix soket bağlantısı açar:

```python
socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
socket.connect("/home/frappe/.omo/agent/rpc/rpc.sock")
```

Bağlantı kurulduktan sonra **mutlaka**:

```json
{"id":"ci","type":"set_client_info","width":100,"capabilities":["question"]}
```

Bu gönderilmezse host `question` yerine `select` + `input` sequential fallback
üretir. Fallback de desteklenir, ancak birincil yol `question` frame'idir.

### 5.2 Oturum açma

Yeni oturum:

```json
{
  "id": "open",
  "type": "open_session",
  "cwd": "/home/frappe/frappe-bench",
  "promptSurface": "terminal",
  "permissionPreset": "full-access",
  "retain_on_disconnect": true,
  "name": "hermes-20261019-1a2b"
}
```

Yanıt:

```json
{
  "id": "open",
  "type": "response",
  "command": "open_session",
  "success": true,
  "data": {
    "sessionId": "rpc-3",
    "state": { "sessionFile": ".../2026-...jsonl", "pendingQuestions": [] }
  }
}
```

**Kritik:** raw `open_session` yanıtında `sessionPath` alanı yoktur.
Kalıcı yol `data.state.sessionFile` içinden alınır. `sessionId` (`rpc-N`) yalnızca
o host epoch'u için geçerlidir; kalıcı anahtar `sessionFile` ve
`data.state.sessionId` (durable session id) değerleridir.

Resume:

```json
{"id":"open","type":"open_session","sessionPath":"<sessionFile>","retain_on_disconnect":true}
```

### 5.3 Prompt

```json
{"id":"p1","type":"prompt","sessionId":"rpc-3","message":"/ulw-plan ..."}
```

Yanıt `data.disposition` = `started` | `queued` | `handled`. `started` normaldir.

### 5.4 Soru frame'i

```json
{
  "type": "extension_ui_request",
  "method": "question",
  "id": "e89993d5-...",
  "requestId": "chatcmpl-tool-...",
  "toolCallId": "chatcmpl-tool-...",
  "waitForAnswer": true,
  "questions": [{
    "id": "q1",
    "header": "Color",
    "question": "Which color do you prefer?",
    "options": [{"label":"Red","description":"..."},{"label":"Blue","description":"..."}],
    "multiSelect": false
  }],
  "timeout": 1800000,
  "deadlineAtMs": 1792440051385,
  "remainingMs": 1800000,
  "sessionId": "rpc-3"
}
```

### 5.5 Cevap

```json
{
  "id": "a1",
  "type": "extension_ui_response",
  "sessionId": "rpc-3",
  "uiRequestId": "e89993d5-...",
  "answers": {"q1": {"selected": ["Red"]}}
}
```

- `sessionId` çoklu-session modda **zorunludur**; eksikse `missing_session_id`.
- `uiRequestId` frame'in `id` alanıdır (eski kısa formda `id` de kabul edilir).
- `question` için `answers` + opsiyonel `comment`; `select`/`input` için `value`;
  `confirm` için `confirmed`.
- Başarılı cevap `question_resolved` yayınlar:
  `{outcome:"answered", resolvedBy:"rpc_connection", answers:{...}}`.
- Geç/tekrar cevap `question_already_resolved` döner; idempotent kabul edilir.

### 5.6 Replay

Bağlantı koptuğunda `retain_on_disconnect: true` sayesinde oturum yaşar.
Yeniden bağlanınca:

1. `extension_ui_request / question` frame'i `open_session` yanıtından **önce**
   gelebilir; client buffer'lamalıdır.
2. `open_session` yanıtındaki `state.pendingQuestions` aynı frame'i içerir.
3. `sessionId` belli olduktan sonra cevap gönderilir.

Spike'ta doğrulandı: soru askıda iken bağlantı kesildi, yeniden bağlanıldı,
`pendingQuestions` replay edildi, cevap verildi, `agent_end` alındı.

### 5.7 Bitiş ve kapatma

- `agent_end` ve `agent_settled` olayları turun bittiğini gösterir.
- Final metin `turn_end` mesajlarının `content[].text` alanlarından toplanır.
- `close_session {sessionId}` ile oturum kapatılır; transkript diskte kalır.
- `closed:false` (başka attachment) fatal değildir.

---

## 6. Veri modeli

`plugin_data_dir("omo")/data.db` — SQLite, WAL modu, thread-safe.

```sql
CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,   -- omo-YYYYMMDD-HHMMSS-xxxx (user-facing)
  hermes_session_id  TEXT,               -- handler'a gelen session_id
  omo_session_id     TEXT,               -- rpc-N (ephemeral, bilgi amaçlı)
  durable_session_id TEXT,               -- data.state.sessionId
  session_path       TEXT,               -- data.state.sessionFile
  cwd                TEXT NOT NULL,
  model              TEXT,
  permission         TEXT NOT NULL,
  status             TEXT NOT NULL,      -- starting|idle|running|question|done|error|cancelled
  pending_request_id TEXT,
  pending_question   TEXT,               -- JSON QuestionPayload
  last_text          TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_hermes
  ON sessions (hermes_session_id, created_at DESC);
```

Kurallar:
- `session_path` dolu olmadan oturum kalıcı sayılmaz.
- `rpc-N` asla resume anahtarı olarak kullanılmaz.
- Satırlar silinmez; `done`/`cancelled` kayıtları `/omo list` için tutulur.
  Temizlik ileride bir `prune` komutuyla yapılır (v1 kapsamı dışı).

---

## 7. Tool sözleşmesi

Tüm handler'lar JSON string döner; asla exception fırlatmaz.

### `omo_start`

```json
{
  "task": "string, zorunlu",
  "cwd": "string, opsiyonel (mutlak yol)",
  "model": "string, opsiyonel (provider/id)",
  "permission": "string, opsiyonel (varsayılan full-access)",
  "name": "string, opsiyonel",
  "parallel": "boolean, opsiyonel (varsayılan false)"
}
```

Döner:

```json
{"ok":true,"session_id":"omo-20261019-1a2b","status":"running","cwd":"...","model":"...","permission":"full-access"}
```

Hata: `session_running` (çalışan oturum var, `parallel=false`), `invalid_cwd`,
`invalid_permission`, `host_unavailable`, `protocol_mismatch`, `omo_error`.

`permission` bilinen preset'lerden biri olmalıdır: `full-access`, `workspace`,
`accept-edits`, `auto`, `read-only`, `ask`. Bilinmeyen değer reddedilir.
`model` verilmezse OmO'nun kendi varsayılan modeli kullanılır. `name` verilmezse
`hermes-<session_id>` üretilir.

### `omo_wait`

```json
{"session_id":"string, opsiyonel","timeout_s":"integer, varsayılan 90, max 120"}
```

Döner:

```json
{
  "ok": true,
  "session_id": "omo-...",
  "status": "question|done|running|idle|error",
  "question": { "...QuestionPayload..." },
  "text": "son anlamlı metin",
  "expires_at": "ISO-8601 (soru varsa)"
}
```

### `omo_answer`

```json
{
  "session_id": "string, opsiyonel",
  "request_id": "string, zorunlu",
  "answers": {"q1": {"selected":["Red"], "text":""}},
  "comment": "string, opsiyonel",
  "timeout_s": "integer, varsayılan 90"
}
```

Cevabı gönderir ve bir sonraki `question` / `done` / `running` durumuna kadar
bekler. Dönen yapı `omo_wait` ile aynıdır.

### `omo_status`

```json
{"session_id":"string, opsiyonel"}
```

Döner: `{ok, session_id, status, cwd, model, permission, elapsed_s, last_text,
pending_question, host: {reachable, pid, generation}}`.

### `omo_cancel`

```json
{"session_id":"string, opsiyonel"}
```

`abort` + `close_session`; durum `cancelled`.

### `omo_resume`

```json
{"session_id":"string, zorunlu","message":"string, opsiyonel"}
```

`session_path` üzerinden yeniden açar. `message` verilirse yeni tur başlatır ve
`running` döner; verilmezse oturum yalnızca attach edilir ve `idle` döner.
Böylece eski bir plan oturumuna yeni bir talimatla devam edilebilir.

### Plugin yapılandırması

`ctx.get_config()` altında, varsayılanlar:

| Anahtar | Varsayılan | Açıklama |
|---------|-----------|----------|
| `default_cwd` | `/home/frappe/frappe-bench` | `cwd` verilmezse kullanılır |
| `permission_default` | `full-access` | `permission` verilmezse kullanılır |
| `allowed_roots` | `[]` | Boş = sınırsız; doluysa `cwd` bu köklerde olmalı |
| `wait_timeout_s` | `90` | `omo_wait` varsayılanı (max 120) |
| `auto_close` | `false` | `true` ise `done` sonrası `close_session`; varsayılanda oturum `idle` kalır |
| `host_socket` | `~/.omo/agent/rpc/rpc.sock` | Unix soket yolu |

`__init__.py` ayrıca `ctx.register_skill("omo-orchestration", ...)` ile paket
skill'ini kaydeder; model `skill_view("omo:omo-orchestration")` ile yükler.

### `/omo` slash komutu

- `/omo status` — aktif oturumun durumu
- `/omo list` — son 10 oturum
- `/omo stop` — aktif oturumu iptal et
- `/omo resume <n|id> [mesaj]` — `/omo list` satır numarası veya id ile oturumu yeniden aç

---

## 8. Soru-cevap akışı

```
model                    plugin                     host                 kullanıcı
  │ omo_start ───────────▶ open_session + prompt ──▶ oturum başlar
  │ omo_wait ────────────▶ attach + oku ──────────▶ question frame
  │ ◀── QuestionPayload ──┘
  │ clarify ─────────────────────────────────────────────────────────▶ soru
  │ ◀────────────────────────────────────────────────────────────── cevap
  │ omo_answer ──────────▶ extension_ui_response ▶ tur devam eder
  │ ◀── sonraki durum ────┘
  │ ... (tekrar)
  │ ◀── done + final text ─┘
```

### `QuestionPayload`

```json
{
  "request_id": "<frame id>",
  "kind": "question|select|input|confirm",
  "questions": [{
    "id": "q1",
    "header": "Approval",
    "question": "Approve the plan?",
    "multiSelect": false,
    "freeText": false,
    "options": [{"label":"Approve","description":"..."}]
  }],
  "timeout_s": 1800,
  "expires_at": "2026-10-19T22:30:00+03:00"
}
```

### Normalizasyon

| Frame | kind | questions | Cevap |
|-------|------|-----------|-------|
| `question` | `question` | frame.questions | `answers` |
| `select` | `select` | tek soru, `title` başlık, options | `value` = seçilen label |
| `input` | `input` | tek soru, `freeText:true` | `value` = metin |
| `confirm` | `confirm` | tek soru, Yes/No | `confirmed` = bool |

Birden fazla `select`/`input` frame'i gelirse (sequential fallback), plugin her
birini ayrı `omo_wait` sonucu olarak döndürür; model sırayla `omo_answer` çağırır.

### `clarify` eşlemesi (skill'de anlatılır)

- Bir `omo_wait` sonucundaki tüm sorular tek `clarify` çağrısında iletilir
  (en fazla 5; fazlası parçalanır).
- `options[].label` değerleri `clarify` seçenekleri olarak **aynen** kullanılır.
- `multiSelect:true` → `clarify` `multi_select:true`.
- `freeText:true` → seçenek verilmez; kullanıcı serbest metin girer.
- Dönen cevap `answers` map'ine çevrilir; serbest metin `text` alanına yazılır.

---

## 9. Yaşam döngüsü ve durum makinesi

```
starting → running ⇄ question → done
              ↘ error          │
              ↘ cancelled      ▼
                          idle ──▶ running (omo_resume + message)
```

- `omo_start`: `starting` → `running`. Prompt kabul edilmezse `error`.
- `omo_wait`: `question` frame'i → `question`; `agent_end`+`agent_settled` → `done`.
  `idle` bir oturumda hemen `idle` döner.
- `omo_answer`: `question` → `running` → (`question` | `done`).
- `omo_cancel`: her durumdan `cancelled`.
- `omo_resume`: `done`/`cancelled` → `idle`; `message` verilirse `running`.
- `done`/`cancelled` sonrası `close_session`; hata olsa bile kayıt korunur.
- Yeni `omo_start` her zaman yeni satır açar; önceki satıra dokunmaz.
- Aynı `hermes_session_id` için `status IN ('starting','running','question')` olan
  bir satır varsa ve `parallel=false` ise `session_running` döner.

### Oturum çözümleme

`session_id` verilmezse:

1. Aynı `hermes_session_id` için `starting|running|question|idle` durumundaki en
   son satır kullanılır.
2. Yoksa aynı `hermes_session_id` için en son satır (done/cancelled dahil) kullanılır.
3. O da yoksa `session_not_found` döner.

`/omo status`, `/omo stop` ve `/omo resume` aynı kuralı izler.

---

## 10. Hata yönetimi

| Kod | Anlam | Davranış |
|-----|-------|----------|
| `host_unavailable` | Soket yok / bağlanamıyor | Bir kez `omo host ensure`, sonra tekrar dene |
| `protocol_mismatch` | `protocolVersion != 1` veya capability eksik | Durdur, sürümü raporla |
| `invalid_cwd` | Yol yok / dizin değil / mutlak değil | Çağrıyı reddet |
| `session_running` | Aynı Hermes oturumunda çalışan oturum | `omo_wait` / `omo_cancel` / `parallel=true` öner |
| `session_not_found` | Kayıt veya `session_path` yok | `/omo list` öner |
| `session_lost` | Transkript bulunamadı | Yeni oturum öner |
| `question_already_resolved` | Geç/tekrar cevap | Güncel state döndür, hata verme |
| `timeout` | `omo_wait` süresi doldu | `status:"running"` + son metin |
| `omo_error` | Host/oturum hatası | `detail` ile birlikte döndür |

Tüm hatalar: `{"ok":false,"error":"<kod>","detail":"...","hint":"..."}`.

---

## 11. Güvenlik

- **Varsayılan `full-access`.** OmO, Hermes kullanıcısının tüm yetkileriyle
  çalışır; dosya sistemi ve shell sınırı yoktur. Bu, tool sonucunda ve
  `/omo status` çıktısında açıkça gösterilir.
- `cwd` doğrulaması: mutlak yol, var olan dizin, `realpath` çözümü.
  Config'te `allowed_roots` verilirse yol bu köklerden birinde olmalıdır;
  varsayılan boştur (sınırsız).
- Prompt'lar ve cevaplar debug log'a yazılmaz; yalnızca uzunluk/özet loglanır.
- State'e secret yazılmaz. Oturum transkripti OmO'nun kendi dizinindedir.
- Soket `0600`, dizin `0700`; aynı kullanıcı sınırı OmO tarafından uygulanır.
- Plugin ayrıcalıklı capability talep etmez; `tools.override` yoktur.
- Handler'lar `try/except` ile sarılır; hata JSON olarak döner.

---

## 12. Test stratejisi

### Birim testleri (host gerektirmez)

- `test_framing.py`: JSONL buffer — parçalı satır, aynı pakette birden fazla satır,
  UTF-8 çok baytlı karakterin ortadan bölünmesi, boş satır.
- `test_questions.py`: `question`/`select`/`input`/`confirm` → `QuestionPayload`;
  cevap map'leme; çok seçimli; serbest metin; bilinmeyen alanlar.
- `test_sessions.py`: durum geçişleri, `session_running` kuralı, `parallel=true`,
  otomatik kapatma, resume kaydı, SQLite atomikliği.

### Entegrasyon testleri (gerçek host, opt-in)

`OMO_INTEGRATION=1` ile çalışır; `omo` yoksa skip.

1. `start → question → answer → done`
2. `start → question → detach → reattach → replay → answer → done`
3. Host restart sonrası `resume` (oturum path'ten açılır)
4. `cancel` mid-turn
5. `session_running` + `parallel=true`
6. `cwd` geçersiz → `invalid_cwd`

### Uçtan uca kabul (v1)

Scratch git repo'da:

1. `omo_start("/ulw-plan create a single-file hello.py")`
2. `omo_wait` → soru gelir (intent/approval); `clarify` ile cevaplanır
3. Akış `done` olana kadar sürer
4. `.omo/drafts/<slug>.md` (veya onay sonrası `.omo/plans/<slug>.md`) oluşmuştur
5. Oturum otomatik kapanmıştır
6. Yeni `omo_start` temiz bir oturum açar (önceki bağlam yok)

### Plugin sağlığı

- `hermes plugins doctor ~/.hermes/plugins/omo`
- `hermes plugins validate ~/.hermes/plugins/omo`
- Hermes açılış süresinde ölçülebilir regresyon yok; host bağlantısı lazy.

---

## 13. Kapsam dışı (v1)

- `omo -p` toplu/CI modu (gelecekte ayrı tool olabilir)
- Dosya/görsel eki gönderme
- Canlı streaming metin (yalnızca soru, durum ve final)
- Çok kullanıcılı yetkilendirme / profil başına ayrı host
- Maliyet bütçesi ve otomatik model seçimi
- Web/desktop UI panelleri

---

## 14. Riskler

| Risk | Etki | Azaltma |
|------|------|---------|
| OmO protokol kayması | Client kırılır | `protocolVersion` + capability kontrolü; sürüm pinle |
| Host daemon kapalı | İlk çağrı gecikir | `ensure` + tek retry; durumu raporla |
| Host ~245 MB RSS | Bellek | Oturum bitince close; host'u plugin durdurmaz |
| `full-access` | Sınırsız yetki | Varsayılanı görünür kıl; `allowed_roots` opsiyonu |
| Model tool şeması tuhaflığı | Soru kaybolur | Her zaman raw `open_session`; CLI open kullanılmaz |
| Uzun `ulw-plan` exploration | Tool timeout | `omo_wait` 90 s parçalı; model döngü kurar |

---

## 15. Uygulama sonrası

Yerel plugin testleri geçtikten sonra:

1. `~/.hermes/plugins/omo/` git deposu olarak kalır (halihazırda `git init`).
2. İstenirse ayrı bir GitHub deposuna taşınır; `hermes plugins install owner/repo`
   ile kurulabilir hale getirilir.
3. `plugin.yaml` sürümü ve README eklenir.
