#include <Arduino.h>
#include <ArduinoOTA.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266WebServer.h>
#include <ESP8266WiFi.h>
#include <Updater.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <sys/time.h>
#include <time.h>
#include "wan_config.h"

// Keep the original wiring: software I2C clock GPIO12, data GPIO14.
U8G2_SSD1306_128X64_NONAME_1_SW_I2C oled(U8G2_R0, 12, 14, U8X8_PIN_NONE);
ESP8266WebServer web(80);

static const char webPage[] PROGMEM = R"HTML(<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WAN 屏显 · 设备管理</title><style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;background:#09151b;color:#eef9f6}
*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 85% 0,#193d43 0,transparent 45%),#09151b}
main{width:min(840px,calc(100% - 32px));margin:0 auto;padding:42px 0 70px}
.eyebrow{color:#7de0c2;letter-spacing:.2em;font-size:11px;font-weight:800}.top{display:flex;align-items:center;justify-content:space-between;gap:15px}
h1{font-size:clamp(27px,5vw,42px);letter-spacing:-.04em;margin:8px 0 6px}.sub{color:#98aeb3;margin:0 0 29px}
.badge{border:1px solid #275f57;background:#173b38;color:#8bf2cd;border-radius:100px;padding:8px 13px;font-size:12px;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{border:1px solid #29414a;border-radius:19px;background:#10242c;padding:24px;box-shadow:0 15px 35px #0002}
.card small{display:block;color:#83a6ac;font-size:12px;letter-spacing:.08em}.ip{font-size:clamp(21px,3.4vw,30px);font-weight:750;margin:17px 0 10px;word-break:break-all;font-variant-numeric:tabular-nums}
.state{color:#73dfb8;font-size:12px}.warn{color:#ffc286}.full{grid-column:1/-1}.meta{display:flex;gap:24px;flex-wrap:wrap;color:#b1c5c7;font-size:13px;margin-top:12px}
h2{font-size:20px;margin:0 0 8px}.help{color:#9eb2b5;font-size:13px;line-height:1.7;margin:0 0 18px}
.file{display:block;border:1px dashed #47756e;border-radius:13px;padding:18px;background:#143038;cursor:pointer;color:#d9eee9}.file input{display:block;margin-top:10px;max-width:100%}
button{margin-top:16px;border:0;background:#70e0ba;color:#08251f;border-radius:11px;padding:12px 19px;font-size:14px;font-weight:800;cursor:pointer}button:disabled{opacity:.5;cursor:wait}
progress{display:none;width:100%;height:10px;margin-top:18px;accent-color:#70e0ba}#message{min-height:20px;color:#a9c5c4;font-size:13px;margin-top:12px}
footer{color:#718f94;font-size:12px;margin-top:22px}@media(max-width:600px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.top{align-items:start;flex-direction:column}}
</style></head><body><main>
<div class="top"><div><div class="eyebrow">WAN SENTINEL / ESP8266</div><h1>设备与固件</h1><p class="sub">双线出口状态与无线升级</p></div><span class="badge" id="connection">正在连接</span></div>
<div class="grid"><section class="card"><small>WAN 1 · 电信</small><div class="ip" id="wan1">—</div><div class="state" id="wan1state">读取中</div></section>
<section class="card"><small>WAN 2 · 联通</small><div class="ip" id="wan2">—</div><div class="state" id="wan2state">读取中</div></section>
<section class="card full"><small>ESP8266 · 局域网</small><div class="ip" id="local">—</div><div class="meta"><span id="clock">系统时间 —</span><span id="uptime">运行时间 —</span><span id="rssi">信号 —</span><span id="build">固件 —</span></div></section>
<section class="card full"><h2>无线更新固件</h2><p class="help">选择 PlatformIO 构建生成的 firmware.bin。上传期间请保持设备供电，完成后设备会自动重启；一般约需 20 秒恢复页面。</p>
<form id="form"><label class="file">固件文件<input id="file" name="firmware" type="file" accept=".bin" required></label><button id="upload" type="submit">上传并更新</button></form><progress id="progress" max="100" value="0"></progress><div id="message" role="status"></div></section></div>
<footer>仅在可信局域网使用 · 设备登录使用 OTA 密码</footer></main><script>
const $=id=>document.getElementById(id);const put=(id,v)=>{$(id).textContent=v};
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);const s=await r.json();
put('connection',s.serverOnline?'监控在线':'监控离线');put('wan1',s.wan1.ip);put('wan1state',s.wan1.state);put('wan2',s.wan2.ip);put('wan2state',s.wan2.state);
put('local',s.localIp);put('clock','系统时间 '+s.time);put('uptime','运行时间 '+s.uptime);put('rssi','Wi-Fi '+s.rssi+' dBm');put('build','固件 '+s.build);
['wan1state','wan2state'].forEach(id=>$(id).className=$(id).textContent==='OK'?'state':'state warn');
}catch(e){put('connection','设备未响应')}}refresh();setInterval(refresh,5000);
$('form').addEventListener('submit',e=>{e.preventDefault();const file=$('file').files[0];if(!file||!file.name.toLowerCase().endsWith('.bin')){put('message','请选择 .bin 固件文件');return}
const data=new FormData();data.append('firmware',file);const x=new XMLHttpRequest();$('upload').disabled=true;$('progress').style.display='block';$('progress').value=0;put('message','正在上传…');
x.open('POST','/update');x.setRequestHeader('X-OTA-Request','wan-display');x.upload.onprogress=e=>{if(e.lengthComputable){$('progress').value=Math.round(e.loaded*100/e.total);put('message','上传 '+$('progress').value+'%')}};
x.onload=()=>{put('message',x.status===200?'更新成功，设备正在重启…':'更新失败：'+x.responseText);$('upload').disabled=false;if(x.status===200)setTimeout(refresh,20000)};
x.onerror=()=>{put('message','连接中断，请等待设备重启后检查版本与状态');$('upload').disabled=false};x.send(data)});
</script></body></html>)HTML";

struct LineStatus {
  String ip = "-";
  String condition = "WAIT";
  long age = -1;
};

LineStatus lines[2];
unsigned long lastPoll = 0;
unsigned long lastPage = 0;
unsigned long lastWifiAttempt = 0;
unsigned long lastDraw = 0;
unsigned long lastUptimeTick = 0;
uint64_t uptimeMillis = 0;
bool hasPolled = false;
bool serverOnline = false;
uint8_t currentPage = 0;
bool wasWifiConnected = false;
bool clockSynced = false;
bool otaReady = false;
bool otaUpdating = false;
bool webReady = false;
bool webUploadAllowed = false;
bool webUploadSucceeded = false;
String webUploadError;

void drawOtaProgress(unsigned percent, const char* title = "OTA UPDATE") {
  oled.firstPage();
  do {
    oled.setFont(u8g2_font_6x12_tf);
    oled.drawStr(4, 13, title);
    oled.drawHLine(4, 17, 120);
    char progress[8];
    snprintf(progress, sizeof(progress), "%u%%", percent);
    oled.setFont(u8g2_font_7x14B_tf);
    oled.drawStr(4, 38, progress);
    oled.drawFrame(4, 46, 120, 12);
    oled.drawBox(7, 49, (percent * 114U) / 100U, 6);
  } while (oled.nextPage());
}

bool parseLine(const String& row, const char* expected, LineStatus& target) {
  const int a = row.indexOf('|');
  const int b = row.indexOf('|', a + 1);
  const int c = row.indexOf('|', b + 1);
  if (a < 0 || b < 0 || c < 0 || row.substring(0, a) != expected) return false;
  String ip = row.substring(a + 1, b);
  String condition = row.substring(b + 1, c);
  if (ip.length() > 15 ||
      (ip != "-" && !IPAddress().fromString(ip)) ||
      (condition != "OK" && condition != "RISK" &&
       condition != "ERROR" && condition != "WAIT")) return false;
  String ageText = row.substring(c + 1);
  ageText.trim();
  if (!ageText.length()) return false;
  for (size_t i = ageText[0] == '-' ? 1 : 0; i < ageText.length(); ++i) {
    if (!isDigit(ageText[i])) return false;
  }
  target.ip = ip;
  target.condition = condition;
  target.age = ageText.toInt();
  return true;
}

void pollMonitor() {
  if (WiFi.status() != WL_CONNECTED) {
    serverOnline = false;
    Serial.printf("WiFi not connected (status=%d)\n", WiFi.status());
    return;
  }
  WiFiClient client;
  HTTPClient http;
  if (!http.begin(client, MONITOR_URL)) {
    serverOnline = false;
    Serial.println(F("Display API URL rejected"));
    return;
  }
  http.setTimeout(5000);
  http.addHeader("X-Device-Token", DISPLAY_TOKEN);
  const int status = http.GET();
  if (status == HTTP_CODE_OK && http.getSize() <= 128) {
    String body = http.getString();
    int split = body.indexOf('\n');
    int end = body.indexOf('\n', split + 1);
    LineStatus wan1, wan2;
    if (split > 0 && end > split &&
        parseLine(body.substring(0, split), "WAN1", wan1) &&
        parseLine(body.substring(split + 1, end), "WAN2", wan2)) {
      const int clockEnd = body.indexOf('\n', end + 1);
      if (clockEnd > end + 6 && body.substring(end + 1, end + 6) == "TIME|") {
        String epochText = body.substring(end + 6, clockEnd);
        bool digits = epochText.length() == 10;
        for (size_t i = 0; i < epochText.length(); ++i) {
          if (!isDigit(epochText[i])) digits = false;
        }
        if (digits) {
          const long epoch = epochText.toInt();
          if (epoch > 1700000000L) {
            struct timeval serverTime = {static_cast<time_t>(epoch), 0};
            settimeofday(&serverTime, nullptr);
          }
        }
      }
      lines[0] = wan1;
      lines[1] = wan2;
      serverOnline = true;
      Serial.printf("WAN1 %s %s | WAN2 %s %s\n", wan1.ip.c_str(),
                    wan1.condition.c_str(), wan2.ip.c_str(), wan2.condition.c_str());
    } else {
      serverOnline = false;
      Serial.println(F("Invalid display response"));
    }
  } else {
    serverOnline = false;
    Serial.printf("Display API result: %d (%s)\n", status,
                  status < 0 ? http.errorToString(status).c_str() : "HTTP response");
  }
  http.end();
}

void drawPage() {
  char clockText[9] = "--:--:--";
  const time_t now = time(nullptr);
  if (now > 1700000000) {
    struct tm localTime;
    localtime_r(&now, &localTime);
    strftime(clockText, sizeof(clockText), "%H:%M:%S", &localTime);
  }
  const uint64_t seconds = uptimeMillis / 1000ULL;
  char uptimeText[20];
  if (seconds >= 86400ULL) {
    const uint64_t days = seconds / 86400ULL;
    if (days > 999ULL) {
      strcpy(uptimeText, "UP 999d+");
    } else {
      snprintf(uptimeText, sizeof(uptimeText), "UP %ud %02uh",
               static_cast<unsigned>(days),
               static_cast<unsigned>((seconds % 86400ULL) / 3600ULL));
    }
  } else {
    snprintf(uptimeText, sizeof(uptimeText), "UP %02u:%02u:%02u",
             static_cast<unsigned>(seconds / 3600ULL),
             static_cast<unsigned>((seconds / 60ULL) % 60ULL),
             static_cast<unsigned>(seconds % 60ULL));
  }
  oled.firstPage();
  do {
    oled.setDrawColor(1);
    oled.drawBox(0, 0, 128, 14);
    oled.setFont(u8g2_font_6x12_tf);
    oled.setDrawColor(0);
    oled.drawStr(4, 11, currentPage == 0 ? "WAN1 / TELECOM" :
                          currentPage == 1 ? "WAN2 / UNICOM" : "ESP8266 / LAN");
    char index[8];
    snprintf(index, sizeof(index), "%u/3", currentPage + 1);
    oled.drawStr(109, 11, index);
    oled.setDrawColor(1);
    String ip;
    const char* state;
    if (currentPage == 2) {
      const bool connected = WiFi.status() == WL_CONNECTED;
      ip = connected ? WiFi.localIP().toString() : "-";
      state = connected ? "WIFI ONLINE" : "WIFI OFFLINE";
    } else {
      const LineStatus& line = lines[currentPage];
      ip = line.ip;
      state = WiFi.status() != WL_CONNECTED ? "WIFI OFFLINE" :
              !serverOnline ? "SERVER OFFLINE" : line.condition.c_str();
    }
    oled.setFont(u8g2_font_7x14B_tf);
    int ipWidth = oled.getStrWidth(ip.c_str());
    oled.drawStr(ipWidth < 128 ? (128 - ipWidth) / 2 : 0, 33, ip.c_str());
    oled.setFont(u8g2_font_6x12_tf);
    const int badgeWidth = oled.getStrWidth(state) + 10;
    if (strcmp(state, "RISK") == 0 || strcmp(state, "ERROR") == 0) {
      oled.drawBox(2, 38, badgeWidth, 15);
      oled.setDrawColor(0);
      oled.drawStr(7, 50, state);
      oled.setDrawColor(1);
    } else {
      oled.drawFrame(2, 38, badgeWidth, 15);
      oled.drawStr(7, 50, state);
    }
    if (currentPage == 2 && WiFi.status() == WL_CONNECTED) {
      char signal[16];
      snprintf(signal, sizeof(signal), "%d dBm", WiFi.RSSI());
      oled.drawStr(128 - oled.getStrWidth(signal) - 2, 50, signal);
    } else if (currentPage < 2 && serverOnline && lines[currentPage].age >= 0) {
      char age[20];
      unsigned long elapsed = static_cast<unsigned long>(lines[currentPage].age) +
                              (millis() - lastPoll) / 1000UL;
      snprintf(age, sizeof(age), "%lus", elapsed);
      oled.drawStr(128 - oled.getStrWidth(age) - 2, 50, age);
    }
    oled.drawHLine(0, 55, 128);
    oled.setFont(u8g2_font_5x8_tf);
    oled.drawStr(1, 63, clockText);
    oled.drawStr(127 - oled.getStrWidth(uptimeText), 63, uptimeText);
  } while (oled.nextPage());
}

bool authorizeWeb() {
  if (web.authenticate("admin", OTA_PASSWORD)) return true;
  web.requestAuthentication();
  return false;
}

void sendWebStatus() {
  if (!authorizeWeb()) return;
  char clockText[24] = "--:--:--";
  const time_t now = time(nullptr);
  if (now > 1700000000) {
    struct tm localTime;
    localtime_r(&now, &localTime);
    strftime(clockText, sizeof(clockText), "%Y-%m-%d %H:%M:%S", &localTime);
  }
  const uint64_t seconds = uptimeMillis / 1000ULL;
  char uptimeText[24];
  snprintf(uptimeText, sizeof(uptimeText), "%lud %02lu:%02lu:%02lu",
           static_cast<unsigned long>(seconds / 86400ULL),
           static_cast<unsigned long>((seconds / 3600ULL) % 24ULL),
           static_cast<unsigned long>((seconds / 60ULL) % 60ULL),
           static_cast<unsigned long>(seconds % 60ULL));
  String response;
  response.reserve(270);
  response += F("{\"wan1\":{\"ip\":\"");
  response += lines[0].ip;
  response += F("\",\"state\":\"");
  response += serverOnline ? lines[0].condition : String(F("SERVER OFFLINE"));
  response += F("\"},\"wan2\":{\"ip\":\"");
  response += lines[1].ip;
  response += F("\",\"state\":\"");
  response += serverOnline ? lines[1].condition : String(F("SERVER OFFLINE"));
  response += F("\"},\"localIp\":\"");
  response += WiFi.localIP().toString();
  response += F("\",\"time\":\"");
  response += clockText;
  response += F("\",\"uptime\":\"");
  response += uptimeText;
  response += F("\",\"rssi\":");
  response += WiFi.RSSI();
  response += F(",\"build\":\"");
  response += F(__DATE__ " " __TIME__);
  response += '"';
  response += F(",\"serverOnline\":");
  response += serverOnline ? F("true}") : F("false}");
  web.sendHeader(F("Cache-Control"), F("no-store"));
  web.send(200, F("application/json; charset=utf-8"), response);
}

void handleWebUpload() {
  HTTPUpload& upload = web.upload();
  if (upload.status == UPLOAD_FILE_START) {
    webUploadAllowed = web.authenticate("admin", OTA_PASSWORD) &&
                       web.header("X-OTA-Request") == "wan-display";
    webUploadSucceeded = false;
    webUploadError = "";
    if (!webUploadAllowed) return;
    if (upload.name != "firmware" || !upload.filename.endsWith(".bin")) {
      webUploadError = F("Only firmware .bin files are supported");
      return;
    }
    const uint32_t freeSpace = (ESP.getFreeSketchSpace() - 0x1000) & 0xFFFFF000;
    if (!Update.begin(freeSpace, U_FLASH)) {
      webUploadError = String(F("Flash init error ")) + Update.getError();
      return;
    }
    otaUpdating = true;
    drawOtaProgress(0, "WEB UPDATE");
    Serial.printf("Web OTA started: %s\n", upload.filename.c_str());
  } else if (upload.status == UPLOAD_FILE_WRITE && webUploadAllowed &&
             webUploadError.isEmpty()) {
    if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
      webUploadError = String(F("Flash write error ")) + Update.getError();
      Update.end();
      otaUpdating = false;
    }
  } else if (upload.status == UPLOAD_FILE_END && webUploadAllowed &&
             webUploadError.isEmpty()) {
    if (Update.end(true)) {
      webUploadSucceeded = true;
      drawOtaProgress(100, "OTA COMPLETE");
      Serial.printf("Web OTA completed: %u bytes\n", upload.totalSize);
    } else {
      webUploadError = String(F("Flash finish error ")) + Update.getError();
      otaUpdating = false;
      drawOtaProgress(0, "OTA FAILED");
    }
  } else if (upload.status == UPLOAD_FILE_ABORTED && webUploadAllowed) {
    if (Update.isRunning()) Update.end();
    webUploadError = F("Upload cancelled");
    otaUpdating = false;
    drawOtaProgress(0, "OTA FAILED");
  }
}

void setupWeb() {
  web.collectHeaders("X-OTA-Request");
  web.on("/", HTTP_GET, []() {
    if (!authorizeWeb()) return;
    web.sendHeader(F("Cache-Control"), F("no-store"));
    web.send_P(200, PSTR("text/html; charset=utf-8"), webPage);
  });
  web.on("/api/status", HTTP_GET, sendWebStatus);
  web.on("/update", HTTP_POST, []() {
    if (!authorizeWeb()) return;
    if (web.header("X-OTA-Request") != "wan-display") {
      web.send(403, F("text/plain; charset=utf-8"), F("Invalid upload request"));
      return;
    }
    if (!webUploadSucceeded) {
      web.send(400, F("text/plain; charset=utf-8"),
               webUploadError.isEmpty() ? F("No valid firmware received") : webUploadError);
      otaUpdating = false;
      return;
    }
    web.send(200, F("text/plain; charset=utf-8"), F("Update complete. Rebooting."));
    delay(250);
    web.client().stop();
    ESP.restart();
  }, handleWebUpload);
}

void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println(F("WAN display boot"));
  oled.begin();
  ArduinoOTA.setHostname("wan-ip-display");
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() {
    otaUpdating = true;
    drawOtaProgress(0);
    Serial.println(F("OTA update started"));
  });
  ArduinoOTA.onProgress([](unsigned progress, unsigned total) {
    if (total == 0) return;
    const unsigned percent = static_cast<unsigned>((static_cast<uint64_t>(progress) * 100U) / total);
    static unsigned lastDrawnStep = 0;
    if (percent == 100 || percent / 5 != lastDrawnStep) {
      lastDrawnStep = percent / 5;
      drawOtaProgress(percent);
    }
  });
  ArduinoOTA.onEnd([]() {
    drawOtaProgress(100, "OTA COMPLETE");
    Serial.println(F("OTA update complete"));
  });
  ArduinoOTA.onError([](ota_error_t error) {
    otaUpdating = false;
    drawOtaProgress(0, "OTA FAILED");
    Serial.printf("OTA update failed: %u\n", static_cast<unsigned>(error));
  });
  setupWeb();
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to SSID: %s\n", WIFI_SSID);
  lastWifiAttempt = millis();
  drawPage();
}

void loop() {
  unsigned long now = millis();
  uptimeMillis += static_cast<uint32_t>(now - lastUptimeTick);
  lastUptimeTick = now;
  const bool connected = WiFi.status() == WL_CONNECTED;
  if (connected != wasWifiConnected) {
    wasWifiConnected = connected;
    serverOnline = false;
    if (connected) {
      hasPolled = false;
      configTime(8 * 3600, 0, "ntp.aliyun.com", "cn.pool.ntp.org");
      if (!otaReady) {
        ArduinoOTA.begin();
        otaReady = true;
        Serial.printf("OTA ready on %s:8266\n", WiFi.localIP().toString().c_str());
      }
      if (!webReady) {
        web.begin();
        webReady = true;
        Serial.printf("Web UI ready at http://%s/\n", WiFi.localIP().toString().c_str());
      }
      Serial.printf("WiFi connected, local IP: %s, RSSI: %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
    } else {
      Serial.printf("WiFi disconnected (status=%d)\n", WiFi.status());
    }
    drawPage();
  }
  if (connected && otaReady) {
    if (webReady) web.handleClient();
    ArduinoOTA.handle();
    if (otaUpdating) return;
  }
  if (!clockSynced && time(nullptr) > 1700000000) {
    clockSynced = true;
    Serial.println(F("System clock synchronized"));
  }
  if (WiFi.status() != WL_CONNECTED && now - lastWifiAttempt >= 15000UL) {
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    lastWifiAttempt = now;
    Serial.println(F("Retrying WiFi connection"));
  }
  if (!hasPolled || now - lastPoll >= 30000UL) {
    hasPolled = true;
    lastPoll = now;
    pollMonitor();
    drawPage();
  }
  if (now - lastPage >= 4000UL) {
    lastPage = now;
    currentPage = (currentPage + 1) % 3;
    drawPage();
  }
  if (now - lastDraw >= 1000UL) {
    lastDraw = now;
    drawPage();
  }
  delay(20);
}
