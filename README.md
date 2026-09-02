# Telegram Storage Photo Gallery

Telegram ne free "unlimited" storage tarike use kari ne, potani branded client
gallery website. Aa folder ma badhu code che — bas Telegram bot banavo ane
run karo.

## Kai rite kaam kare che

```
Client photos --> aapno Telegram bot --> tamari private Telegram channel (storage)
                                              |
                                              v
                          Aa website (Flask app) photos fetch kari
                          gallery grid ma batave che, client ne
                          password thi access, favorite, download
```

Telegram token client ne kadi dekhatu nathi — server backend ma j rahe che.

## Step 1 — Bot banavo (5 minute)

1. Telegram kholo, search karo **@BotFather**
2. `/newbot` mokalo, naam apo (jem "MyStudioGalleryBot")
3. Ek token male — aavu dekhase: `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx`
   — aa safe rakho, koine na apso

## Step 2 — Private channel banavo

1. Telegram ma naya **Channel** banavo (Private rakho)
2. Channel settings > Administrators > tamara bot ne **Add Admin** karo
3. Channel ma ek test message mokalo (kai pan)
4. Browser ma aa URL kholo (TOKEN badli devanu):
   `https://api.telegram.org/botTOKEN/getUpdates`
5. Response ma `"chat":{"id": -1001234567890...}` jevu dekhase — e number
   (minus sathe) tamaru `CHANNEL_ID` che

## Step 3 — Local setup

```bash
cd telegram-gallery
cp .env.example .env
# .env file kholo, BOT_TOKEN, CHANNEL_ID, ADMIN_KEY bharo

pip install -r requirements.txt
python server.py
```

Browser ma kholo:
- `http://localhost:5000/admin` — client galleries banavva/manage karva mate
- `http://localhost:5000/gallery/<slug>` — client jevu gallery jue e rite

## Step 4 — Client mate gallery banavvi

1. `/admin` ma jaine login karo (ADMIN_KEY thi)
2. "Create gallery" ma client nu naam ane password nakho
3. Gallery kholo, photos drag-drop karo — automatically Telegram par upload
   thai jashe
4. Client ne link mokalo: `yourwebsite.com/gallery/priya-arjun-wedding`
   sathe password

## Step 5 — Free hosting par live karo (client ne link aapva mate)

Localhost link client ne kaam nahi lage — internet par host karvu padse.
Bannem free che:

**Render.com** (recommended, sauthi simple)
1. Aa folder GitHub repo ma push karo
2. Render.com par "New Web Service" banavo, repo connect karo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python server.py`
5. Environment variables ma BOT_TOKEN, CHANNEL_ID, ADMIN_KEY, SECRET_KEY nakho
   (`.env` file upload nathi karvani, dashboard ma manually nakhvi)
6. Deploy — free URL male (jem `yourapp.onrender.com`)

**Railway.app** — same process, thoda faster free tier

## Dhyan rakhva jevi vaato

- **Free tier limitations**: Render/Railway free plan par app "sleep" thai
  shake thoda time nahi vaparo to — pehli request ma 20-30 second lagi shake
  jagva mate. Business grow thay pachi paid tier (~$5-7/month) thi aa
  problem jashe.
- **Telegram file size limit**: Bot API thi ek photo 20MB sudhi j upload
  thai shake — professional JPEGs mate saras che, pan RAW files nahi.
- **Storage khali kem che**: Telegram par photos "channel messages" tarike
  store thay che, temna file_id hamesha valid rahe che jya sudhi channel/bot
  delete na karo.
- **Backup**: `data/galleries.json` file ma tamari badhi gallery info
  (kai photo kai client ni) store thay che — aa file nu backup rakhjo.

## Su next add kari shakay

- Bulk download (zip) button
- Client selection/comment feature
- Custom domain jodvu (Render/Railway par free ma kari shakay)
- Watermark automatically add karvu upload vate

Koi step ma atki jao to puchi shakay che.
