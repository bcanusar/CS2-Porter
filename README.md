# CS2 Porter

CS2 Porter turns Counter-Strike: Source and CS:GO maps (`.bsp`) into Counter-Strike 2 addons
that you can open in Hammer. It is made with bhop maps in mind, but it works on other maps too.

For each map it:

- opens the `.bsp` and imports it into a CS2 addon;
- finds the materials, models, sounds and particles the map uses, in the map itself and in your
  installed Source 1 games, and converts them;
- fixes the things that break in CS2: lights, triggers, ladders, skybox, water, ropes, bhop
  blocks and teleports, timer zones and more.

The map itself is **not** compiled. You open the result in Hammer and compile it there.

## Requirements

- Windows 10 or 11 (64 bit).
- **Counter-Strike 2** with the **Counter-Strike 2 Workshop Tools**. In Steam, open
  Counter-Strike 2 > Properties > DLC and tick "Counter-Strike 2 Workshop Tools".
- Recommended: the Source 1 games the maps were made for, installed through Steam
  (Counter-Strike: Source, Half-Life 2, CS:GO and so on). Their materials and models are found
  automatically. Without them, only the files packed into the map can be converted.
- Free disk space: the work files of a big map take about 200 MB, the addon a few hundred MB.

## Installing

1. Download `CS2-Porter-<version>.zip` from the Releases page and extract it anywhere,
   for example to `Documents\CS2 Porter`. Keep the `tools` folder next to `CS2 Porter.exe`:
   it holds the map decompiler (BSPSource, with its own Java) and VTFCmd.
2. Run `CS2 Porter.exe`.

When the program starts, the log tells you if CS2, the Workshop Tools or the map decompiler are
missing.

Windows SmartScreen may warn about the program because it is not signed. Click
"More info" > "Run anyway". You can also build the exe yourself (see below).

## Using it

1. **Port Map** tab: add one or more `.bsp` files.
2. Select a map and pick the addon it goes into, or type a new addon name.
3. Press **Port**. Finished maps turn green.
4. Open the addon in Hammer (CS2 Workshop Tools), open the map and compile it.

Other tabs:

- **Missing Assets**: lists what a ported map still misses, and finds a file in your games, in
  CS2 or in a `.bsp`.
- **Tools**: converts single files or folders (VMT to VMAT, MDL to VMDL and more).
- **Settings**: language, theme, the CS2 folder, extra folders with Source 1 files, and the
  port options.

## Getting good results

- **Close CS2 and Hammer** while a map is being ported.
- Install the Source 1 game the map was made for. Most "missing" materials and models come from
  a game that is not installed.
- Missing files are shown in red. You can add a folder with your own Source 1 files under
  Settings > Resource folders, then port the map again with "Overwrite existing files" on.
- Leave "Overwrite existing files" off if you have edited the addon by hand. Turn it on to
  replace everything with a fresh port.
- After porting, compile the map in Hammer with lighting and cubemaps on. Reflections, light
  probes and water only look right after a full compile.

## Where things go

- The addon: `Counter-Strike Global Offensive\content\csgo_addons\<addon>` and
  `...\game\csgo_addons\<addon>`.
- Work files: `%LOCALAPPDATA%\CS2Porter\work`. They can be deleted any time from
  Settings > Work files.
- Settings: `settings.json` next to the program.

The program only writes into your addon folders, its work folder and its own folder. It does
not change CS2's own game files.

## Internet use

Only one thing goes online: when the "bhop script" option is on and the map needs it, the
program downloads the newest version of the bhop script with one HTTPS request and keeps a copy.
Without a connection the saved copy, or the copy built into the program, is used. Turn off
"Download the newest bhop script" in Settings to stay fully offline.

## Troubleshooting

| Problem | What to do |
|---|---|
| "CS2 folder not found" | Pick the `Counter-Strike Global Offensive` folder in Settings. |
| "Workshop Tools are not installed" | Install the DLC as described in Requirements. |
| "Map decompiler not found" | The `tools\BSPSource` folder is missing. Extract the whole release zip again, or see `tools\README.md`. |
| Many materials or models missing | Install the game the map was made for, or add a resource folder. |

## Running from source

Needs Python 3.10 or newer (developed and tested with 3.14) and Windows.

```
pip install -r requirements.txt
python "CS2 Porter.pyw"
```

`CS2 Porter.bat` does the same with a double click. The repository does not hold the third
party tools: put BSPSource (and optionally VTFCmd) into `tools` as `tools\README.md` explains.

## Building the exe

Put the tools into `tools` (see `tools\README.md`), then run `build.bat`. The first run makes a
`.venv` and installs the build requirements. It writes `build\dist\CS2 Porter.exe` and
`release\CS2-Porter-<version>.zip` with the program, the tools and their licenses.

## Legal

CS2 Porter is a fan made tool. It is not made, endorsed or supported by Valve. Counter-Strike,
Source, Steam and Hammer are trademarks of Valve Corporation.

Only port maps you have the right to use. If you upload a ported map to the Steam Workshop, get
the permission of the original map author and follow the Steam Workshop rules.

The release includes BSPSource and VTFCmd unchanged. Their licenses and source code links are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and the `licenses` folder. The bhop script that
the program adds to bhop maps is made by tehlikeli91, the author of CS2 Porter.

## License

MIT, see [LICENSE](LICENSE). Made by tehlikeli91.
