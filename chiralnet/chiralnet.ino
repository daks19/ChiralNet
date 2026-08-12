#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPping.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <HTTPClient.h>

Preferences prefs;
WebServer configServer(80);

String cfgSSID, cfgPassword, cfgMqttServer;
const int mqtt_port = 1883;
const char* mqtt_topic = "chiralnet/telemetry";

const char* repeater_ap_ssid_prefix = "ChiralNet-Relay-";
const char* repeater_ap_password = "chiralnet123";

WiFiClient espClient;
PubSubClient client(espClient);

IPAddress pingTarget;
String deviceID;
String commandTopic;
String currentMode = "monitor";
bool staConnected = false;

bool isSwarmMaster = false;
unsigned long swarmStartTime = 0;

// ---------------------------------------------------------------
// Config page & Swarm Provisioning Endpoint
// ---------------------------------------------------------------
void handleConfigRoot() {
  String html = "<html><body style='font-family:sans-serif;max-width:400px;margin:40px auto;'>";
  html += "<h2>ChiralNet Node Config</h2>";
  html += "<p>Device: " + deviceID + "</p>";
  html += "<form method='POST' action='/save'>";
  html += "WiFi SSID:<br><input name='ssid' value='" + cfgSSID + "' style='width:100%;padding:6px;'><br><br>";
  html += "WiFi Password:<br><input name='password' type='password' value='' placeholder='(leave blank to keep current)' style='width:100%;padding:6px;'><br><br>";
  html += "MQTT Broker IP:<br><input name='mqtt' value='" + cfgMqttServer + "' style='width:100%;padding:6px;'><br><br>";
  html += "<button type='submit' style='padding:8px 20px;'>Save & Restart</button>";
  html += "</form></body></html>";
  configServer.send(200, "text/html", html);
}

void handleConfigSave() {
  String newSSID = configServer.arg("ssid");
  String newPassword = configServer.arg("password");
  String newMqtt = configServer.arg("mqtt");

  prefs.putString("ssid", newSSID);
  if (newPassword.length() > 0) prefs.putString("password", newPassword);
  prefs.putString("mqtt", newMqtt);

  configServer.send(200, "text/html",
    "<html><body style='font-family:sans-serif;text-align:center;margin-top:100px;'>"
    "<h3>Saved. Restarting...</h3></body></html>");
  delay(1500);
  ESP.restart();
}

void startConfigServer() {
  configServer.on("/", handleConfigRoot);
  configServer.on("/save", HTTP_POST, handleConfigSave);
  
  // Endpoint for blank nodes to fetch credentials during Swarm Provisioning
  configServer.on("/provision_data", HTTP_GET, []() {
    String json = "{\"ssid\":\"" + cfgSSID + "\",\"password\":\"" + cfgPassword + "\",\"mqtt\":\"" + cfgMqttServer + "\"}";
    configServer.send(200, "application/json", json);
  });
  
  configServer.begin();
}

// ---------------------------------------------------------------
// WiFi connect using saved credentials
// ---------------------------------------------------------------
bool connectWiFi() {
  if (cfgSSID.length() == 0) return false;

  Serial.print("Connecting to: ");
  Serial.println(cfgSSID);
  WiFi.mode(currentMode == "repeater" ? WIFI_AP_STA : WIFI_STA);
  WiFi.begin(cfgSSID.c_str(), cfgPassword.c_str());

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("Wi-Fi Connected!");
    Serial.print("Node IP (visit this to reconfigure anytime): ");
    Serial.println(WiFi.localIP());
    pingTarget = WiFi.gatewayIP();
    return true;
  }
  Serial.println("Failed to connect with saved credentials.");
  return false;
}

// ---------------------------------------------------------------
// Fallback: own setup AP if no saved creds work
// ---------------------------------------------------------------
void startSetupAP() {
  String apName = "ChiralNet-Setup-" + deviceID;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(apName.c_str());
  Serial.println("=================================================");
  Serial.print("Could not connect. Setup AP started: ");
  Serial.println(apName);
  Serial.println("Connect to it, then visit http://192.168.4.1");
  Serial.println("to enter your real WiFi + MQTT settings.");
  Serial.println("=================================================");
}

// ---------------------------------------------------------------
// MQTT / telemetry
// ---------------------------------------------------------------
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String msg;
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  msg.trim();
  msg.toLowerCase();
  Serial.print("Command received: ");
  Serial.println(msg);

  if (msg == "repeater" && currentMode != "repeater") {
    currentMode = "repeater";
    WiFi.mode(WIFI_AP_STA);
    String apName = repeater_ap_ssid_prefix + deviceID;
    int curChannel = WiFi.channel();
    if (curChannel == 0) curChannel = 1;
    bool apRes = WiFi.softAP(apName.c_str(), repeater_ap_password, curChannel);
    Serial.print("Repeater AP start result: ");
    Serial.print(apRes ? "SUCCESS: " : "FAILED: ");
    Serial.println(apName);
  } else if (msg == "monitor" && currentMode != "monitor") {
    currentMode = "monitor";
    if (!isSwarmMaster) {
      WiFi.softAPdisconnect(true);
      WiFi.mode(WIFI_STA);
      connectWiFi();
    }
  } else if (msg == "swarm") {
    Serial.println("Starting Swarm Provisioning AP for 5 minutes...");
    isSwarmMaster = true;
    swarmStartTime = millis();
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP("ChiralNet-Prov", "meshprov123");
  }
}

void connectMQTT() {
  if (cfgMqttServer.length() == 0) return;
  while (!client.connected()) {
    Serial.print("Connecting to MQTT broker...");
    String clientId = "ChiralNetNode-" + deviceID;
    if (client.connect(clientId.c_str())) {
      Serial.println(" connected!");
      client.subscribe(commandTopic.c_str());
      client.subscribe("chiralnet/commands/all"); // Listen for broadcast commands like 'swarm'
    } else {
      Serial.print(" failed, rc=");
      Serial.print(client.state());
      Serial.println(" retrying in 3 seconds");
      delay(3000);
      return;  
    }
  }
}

int scanNearbyAPs() {
  if (currentMode == "repeater") return 0; // Skip scanning in AP mode to prevent AP drop
  return WiFi.scanNetworks(false, false, false, 150);
}

void publishTelemetry() {
  float avgLatency = -1;
  float packetLoss = 100;
  int successCount = 0;
  float totalTime = 0;
  bool result = Ping.ping(pingTarget, 1);
  if (result) {
    successCount = 1;
    totalTime = Ping.averageTime();
  }
  avgLatency = (successCount > 0) ? totalTime : -1;
  packetLoss = (successCount > 0) ? 0 : 100;

  JsonDocument doc;
  doc["device_id"]   = deviceID;
  doc["ssid"]        = WiFi.SSID();
  doc["rssi"]        = WiFi.RSSI();
  doc["channel"]     = WiFi.channel();
  doc["latency_ms"]  = avgLatency;
  doc["packet_loss"] = packetLoss;
  doc["nearby_aps"]  = scanNearbyAPs();
  doc["timestamp"]   = millis();
  doc["mode"]        = currentMode;

  String jsonOutput;
  serializeJson(doc, jsonOutput);
  Serial.println(jsonOutput);
  client.publish(mqtt_topic, jsonOutput.c_str());
}

// ---------------------------------------------------------------
// Swarm Provisioning: scan for Master, fetch creds, save & reboot
// ---------------------------------------------------------------
bool trySwarmProvision() {
  WiFi.disconnect();
  WiFi.mode(WIFI_STA);
  delay(100);
  
  Serial.println("Scanning for ChiralNet-Prov...");
  int n = WiFi.scanNetworks();
  bool foundSwarm = false;
  for (int i = 0; i < n; ++i) {
    if (WiFi.SSID(i) == "ChiralNet-Prov") {
      foundSwarm = true;
      break;
    }
  }
  
  if (!foundSwarm) {
    Serial.println("No Swarm Master found nearby.");
    return false;
  }
  
  Serial.println("Swarm Master found! Connecting...");
  WiFi.begin("ChiralNet-Prov", "meshprov123");
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500); Serial.print("."); attempts++;
  }
  
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nFailed to connect to Swarm Master AP.");
    return false;
  }
  
  Serial.println("\nConnected to Master. Fetching credentials...");
  HTTPClient http;
  http.begin("http://192.168.4.1/provision_data");
  int httpCode = http.GET();
  
  if (httpCode == 200) {
    String payload = http.getString();
    JsonDocument doc;
    deserializeJson(doc, payload);
    
    prefs.putString("ssid", doc["ssid"].as<String>());
    prefs.putString("password", doc["password"].as<String>());
    prefs.putString("mqtt", doc["mqtt"].as<String>());
    
    Serial.println("Swarm config saved! Rebooting to join main mesh...");
    http.end();
    delay(1000);
    ESP.restart();
  }
  
  Serial.println("Failed to fetch data from Master.");
  http.end();
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("\n\n========== ChiralNet Node Booting ==========");

  prefs.begin("chiralnet", false);
  cfgSSID = prefs.getString("ssid", "");
  cfgPassword = prefs.getString("password", "");
  cfgMqttServer = prefs.getString("mqtt", "");

  uint64_t chipid = ESP.getEfuseMac();
  char macBuf[13];
  snprintf(macBuf, sizeof(macBuf), "%04X%08X",
           (uint16_t)(chipid >> 32), (uint32_t)chipid);
  deviceID = String(macBuf);

  WiFi.mode(WIFI_STA); 
  Serial.print("Device ID: ");
  Serial.println(deviceID);
  Serial.print("Saved SSID: ");
  Serial.println(cfgSSID.length() > 0 ? cfgSSID : "(none)");
  Serial.print("Saved MQTT: ");
  Serial.println(cfgMqttServer.length() > 0 ? cfgMqttServer : "(none)");
  commandTopic = "chiralnet/commands/" + deviceID;

  // -------------------------------------------------------------------
  // Swarm Provisioning Client Logic (reusable)
  // Scans for a Master Node to download credentials over-the-air
  // -------------------------------------------------------------------
  bool swarmSuccess = false;
  if (cfgSSID.length() == 0) {
    Serial.println("No saved config. Scanning for Swarm Master...");
    swarmSuccess = trySwarmProvision();
  }

  // Normal Boot Process
  if (!swarmSuccess) {
    staConnected = connectWiFi();
    
    // If saved creds failed, try Swarm before falling back to captive portal
    if (!staConnected) {
      Serial.println("Saved creds failed. Scanning for Swarm Master...");
      swarmSuccess = trySwarmProvision();
      if (!swarmSuccess) {
        startSetupAP();
      }
    }
  }

  startConfigServer(); 

  if (staConnected && cfgMqttServer.length() > 0) {
    client.setServer(cfgMqttServer.c_str(), mqtt_port);
    client.setCallback(onMqttMessage);
  }

  Serial.println("========== Setup complete ==========");
}

void loop() {
  configServer.handleClient(); 

  // Auto-disable Swarm Master AP after 5 minutes to clean up airwaves
  if (isSwarmMaster && (millis() - swarmStartTime > 300000)) {
    Serial.println("Swarm Provisioning timeout (5 min). Disabling AP.");
    isSwarmMaster = false;
    if (currentMode == "monitor") {
      WiFi.softAPdisconnect(true);
      WiFi.mode(WIFI_STA);
    }
  }

  if (!staConnected) return; 

  if (currentMode == "monitor" && WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (cfgMqttServer.length() > 0) {
    if (!client.connected()) connectMQTT();
    client.loop();
  }

  publishTelemetry();

  delay(250);
}
