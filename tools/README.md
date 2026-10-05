# tools

CS2 Porter looks for its helper programs in this folder. The release zip already has them;
this is only needed when you run from source or build the release yourself.

## BSPSource (required)

1. Open https://github.com/ata4/bspsrc/releases and download the Windows zip (1.4.8 is the
   version the release is made with).
2. Extract it into `tools/BSPSource`.

A folder inside `BSPSource` is fine too, for example `tools/BSPSource/bspsrc-windows/`. The
program finds the folder that has `bin/java.exe` and `lib/modules`. The Windows release comes
with its own Java, so you do not need to install Java.

## VTFCmd (optional)

The program reads VTF textures by itself. VTFCmd is only used for the rare formats it cannot
read. Put `VTFCmd.exe`, `VTFLib.dll` and `DevIL.dll` into `tools/VTFCmd/`. They come from
VTFCmd Reloaded: https://github.com/misyltoad/VTFLib

Licenses of both are listed in `THIRD_PARTY_NOTICES.md`.
