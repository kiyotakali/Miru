import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.miru.companion',
  appName: 'Miru',
  webDir: 'www',
  // Allow loading external URLs (the Flask backend)
  server: {
    allowNavigation: ['*'],
    cleartext: true,  // Allow http:// for LAN connections
  },
  android: {
    // Allow mixed content (http resources on https pages)
    allowMixedContent: true,
    // Keep WebView inspection available for APK QA.
    webContentsDebuggingEnabled: true,
  },
  plugins: {
    PushNotifications: {
      presentationOptions: ['badge', 'sound', 'alert'],
    },
  },
};

export default config;
