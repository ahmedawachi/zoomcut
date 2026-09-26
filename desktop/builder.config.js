// How the desktop app is packaged: a native window (Electron) around the same
// Zoomcut the command line runs, with its own ffmpeg. The version is the
// Python package's, so the app and `pip install zoomcut` never disagree.
'use strict';

const fs = require('node:fs');
const path = require('node:path');

const version = fs.readFileSync(path.join(__dirname, '..', 'pyproject.toml'), 'utf8')
  .match(/^version\s*=\s*"([^"]+)"/m)[1];

// Signing is optional. With a Developer ID (CSC_LINK / CSC_KEY_PASSWORD) and
// Apple notarization credentials (APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD,
// APPLE_TEAM_ID) macOS builds are signed and notarized; a Windows certificate
// signs the installer the same way. Without them the Mac app is ad-hoc signed,
// which Apple Silicon needs just to run it.
const signed = Boolean(process.env.CSC_LINK || process.env.CSC_NAME);
const notarize = signed && Boolean(process.env.APPLE_TEAM_ID);

const videoTypes = ['mov', 'mp4', 'm4v', 'mkv', 'webm'];

module.exports = {
  appId: 'io.github.ahmedawachi.zoomcut',
  productName: 'Zoomcut',
  copyright: 'Copyright © 2026 Ahmed Awachi — MIT licensed. Includes ffmpeg, GPL.',
  extraMetadata: { version },
  // resources/, not build/: the repository ignores every build/ folder
  directories: { output: 'dist', buildResources: 'resources' },
  files: ['main.js', 'preload.js', 'loading.html', 'package.json'],
  extraResources: [
    // the Zoomcut server and its ffmpeg, as packaging/build_desktop.py stages them
    { from: '../dist/zoomcut', to: 'server' },
    // the window icon Linux desktops show (made by tools/make_brand.py)
    { from: '../docs/zoomcut.png', to: 'icon.png' },
  ],
  asar: true,
  // Chromium's own UI strings in English only; the editor has no others
  electronLanguages: ['en', 'en-US', 'en_US'],
  artifactName: 'Zoomcut-${version}-${os}-${arch}.${ext}',
  publish: null,

  mac: {
    target: [{ target: 'dmg', arch: ['arm64'] }],
    category: 'public.app-category.video',
    icon: '../docs/zoomcut.icns',
    identity: signed ? undefined : '-',
    hardenedRuntime: signed,
    gatekeeperAssess: false,
    entitlements: signed ? 'resources/entitlements.mac.plist' : undefined,
    entitlementsInherit: signed ? 'resources/entitlements.mac.plist' : undefined,
    notarize,
    extendInfo: {
      // "Open with Zoomcut" and dropping a recording on the Dock icon, without
      // ever making Zoomcut the default player for anyone's videos
      CFBundleDocumentTypes: [{
        CFBundleTypeName: 'Screen recording',
        CFBundleTypeRole: 'Viewer',
        LSHandlerRank: 'Alternate',
        CFBundleTypeExtensions: videoTypes,
      }],
    },
  },
  dmg: {
    sign: false,
    window: { width: 560, height: 380 },
    contents: [
      { x: 150, y: 190 },
      { x: 410, y: 190, type: 'link', path: '/Applications' },
    ],
  },

  win: {
    target: [{ target: 'nsis', arch: ['x64'] }],
    icon: '../docs/zoomcut.ico',
  },
  nsis: {
    oneClick: true,
    perMachine: false,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: 'Zoomcut',
    uninstallDisplayName: 'Zoomcut',
    deleteAppDataOnUninstall: false,
  },

  linux: {
    target: [{ target: 'AppImage', arch: ['x64'] }, { target: 'deb', arch: ['x64'] }],
    icon: '../docs/zoomcut.png',
    category: 'AudioVideo',
    synopsis: 'Record your screen, get a video that looks edited',
    description: 'Zoomcut records your screen and moves a virtual camera to what matters, then holds perfectly still.',
    maintainer: 'Ahmed Awachi <ahawachi@al-amthal.com>',
    executableName: 'zoomcut-desktop',
  },
  deb: {
    // window picking uses one of these; recording a region works without
    recommends: ['wmctrl', 'xdotool'],
  },
};
