#!/bin/sh
# Create Chrome web-app .desktop entries so web-only services appear in the
# Apps menu like native apps and open in their own clean window
# (Chrome --app mode). Also fetches each service's logo (128px favicon) into
# the hicolor icon theme so the Apps menu shows real icons.
# Re-running is safe: icons are only downloaded if missing.
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/128x128/apps"
mkdir -p "$APPS_DIR" "$ICON_DIR"

mkwebapp() {
  # mkwebapp <file> <Name> <URL> <Category> <icon-name> [favicon-domain] [icon-url]
  cat > "$APPS_DIR/$1" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Comment=$2 (web app)
Exec=google-chrome --app=$3 --class=$5
Terminal=false
Categories=$4;
Icon=$5
StartupWMClass=$5
EOF
  # logo: prefer an exact product logo URL; else 128px favicon from Google's
  # favicon service (well-cached, no scraping). Icons only download if missing.
  if [ ! -s "$ICON_DIR/$5.png" ]; then
    if [ -n "$7" ]; then
      curl -sL --max-time 10 -o "$ICON_DIR/$5.png" "$7" || rm -f "$ICON_DIR/$5.png"
    elif [ -n "$6" ]; then
      curl -sL --max-time 10 -o "$ICON_DIR/$5.png" "https://www.google.com/s2/favicons?domain=$6&sz=128" || rm -f "$ICON_DIR/$5.png"
    fi
  fi
}

# --- social ---
mkwebapp instagram.desktop  "Instagram"    "https://www.instagram.com/"            Network    instagram     instagram.com
mkwebapp x.desktop          "X"            "https://x.com/"                        Network    x             x.com
mkwebapp whatsapp.desktop   "WhatsApp"     "https://web.whatsapp.com/"             Network    whatsapp      web.whatsapp.com
mkwebapp telegram.desktop   "Telegram"     "https://web.telegram.org/k/"           Network    telegram      telegram.org
mkwebapp reddit.desktop     "Reddit"       "https://www.reddit.com/"               Network    reddit        reddit.com
mkwebapp facebook.desktop   "Facebook"     "https://www.facebook.com/"             Network    facebook      facebook.com
mkwebapp tiktok.desktop     "TikTok"       "https://www.tiktok.com/"               Network    tiktok        tiktok.com
mkwebapp threads.desktop    "Threads"      "https://www.threads.net/"              Network    threads       threads.net

# --- music & video ---
mkwebapp applemusic.desktop "Apple Music"  "https://music.apple.com/"              AudioVideo applemusic     music.apple.com
mkwebapp spotify.desktop    "Spotify"      "https://open.spotify.com/"             AudioVideo spotify        spotify.com
mkwebapp youtube.desktop    "YouTube"      "https://www.youtube.com/"              AudioVideo youtube        youtube.com
mkwebapp netflix.desktop    "Netflix"      "https://www.netflix.com/"              AudioVideo netflix        netflix.com
mkwebapp primevideo.desktop "Prime Video"  "https://www.amazon.com/primevideo/"    AudioVideo primevideo     primevideo.com
mkwebapp disneyplus.desktop "Disney+"      "https://www.disneyplus.com/"           AudioVideo disneyplus     disneyplus.com
mkwebapp twitch.desktop     "Twitch"       "https://www.twitch.tv/"                AudioVideo twitch         twitch.tv
mkwebapp soundcloud.desktop "SoundCloud"   "https://soundcloud.com/"               AudioVideo soundcloud     soundcloud.com

# --- Google suite ---
# NOTE: Docs/Sheets/Slides share the docs.google.com host, so their favicon
# would all be the same Docs triangle — use exact product logos instead.
_G="https://ssl.gstatic.com/images/branding/product/1x"
mkwebapp gmail.desktop      "Gmail"        "https://mail.google.com/"              Office     gmail          mail.google.com      "$_G/gmail_2020q4_48dp.png"
mkwebapp sheets.desktop     "Google Sheets" "https://docs.google.com/spreadsheets" Office     sheets         docs.google.com      "$_G/sheets_2020q4_48dp.png"
mkwebapp docs.desktop       "Google Docs"  "https://docs.google.com/document"      Office     docs           docs.google.com      "$_G/docs_2020q4_48dp.png"
mkwebapp slides.desktop     "Google Slides" "https://docs.google.com/presentation" Office     slides         docs.google.com      "$_G/slides_2020q4_48dp.png"
mkwebapp drive.desktop      "Google Drive" "https://drive.google.com/"             Office     drive          drive.google.com     "$_G/drive_2020q4_48dp.png"
mkwebapp calendar.desktop   "Google Calendar" "https://calendar.google.com/"       Office     calendar       calendar.google.com  "$_G/calendar_2020q4_48dp.png"
mkwebapp photos.desktop     "Google Photos" "https://photos.google.com/"           Graphics   photos         photos.google.com
mkwebapp maps.desktop       "Google Maps"  "https://maps.google.com/"              Utility    maps           maps.google.com

# --- work & AI ---
mkwebapp notion.desktop     "Notion"       "https://www.notion.so/"                Office     notion         notion.so
mkwebapp figma.desktop      "Figma"        "https://www.figma.com/"                Graphics   figma          figma.com
mkwebapp chatgpt.desktop    "ChatGPT"      "https://chatgpt.com/"                  Office     chatgpt        chatgpt.com
mkwebapp gemini.desktop     "Gemini"       "https://gemini.google.com/"            Office     gemini         gemini.google.com

# --- utilities ---
mkwebapp translate.desktop  "Google Translate" "https://translate.google.com/"     Utility    translate      translate.google.com
mkwebapp wikipedia.desktop  "Wikipedia"    "https://www.wikipedia.org/"            Education  wikipedia      wikipedia.org

rm -f "$APPS_DIR"/*.desktop.desktop
echo "web apps: $(ls "$APPS_DIR"/*.desktop 2>/dev/null | wc -l) | icons: $(ls "$ICON_DIR"/*.png 2>/dev/null | wc -l)"