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

## Features

A plain import of a map into CS2 brings over its brushes and entities and leaves the rest to
you. CS2 Porter goes further:

| Area | What CS2 Porter does |
|---|---|
| Many maps at once | A list of maps, each into its own addon or a shared one. Two maps are ported at a time and the next starts as soon as one ends. Maps can come as `.bsp`, `.vmf`, `.bsp.bz2` or inside `.zip`, `.rar` and `.7z` archives. |
| Finding assets | Looks for every material, model, sound and particle in the map's own packed files, in every installed Source 1 game (folders and VPKs) and in your own resource folders. Maps placed in a resource folder are read as packages too, so files another map packed can fill the gaps. Materials the map forgot to pack are taken from the copy the map compiler left inside the map, and damaged packed files are recovered when a good copy exists. |
| Models | Its own model decompiler turns `.mdl` files into CS2 models with their physics, skins and correct orientation; animated models keep their animations. |
| Materials | Translucency, alpha test, self illumination, additive glow, color tints, scrolling and animated textures (turned into sprite sheets), blend materials, graffiti decals, wireframe materials, water and refracting glass are converted to their CS2 shaders. Textures that fade out with distance in Source 1 fade out in CS2 too. Texture scales and positions on brushes are corrected. |
| Lighting | Light brightness is measured against the Source 1 falloff and matched in lumens, the sun and sky keep their strength, and materials that gave light in Source 1 (texture lights) glow and light the map again. Dynamic lights become baked lights, lights CS2 would not bake are moved to a spot where they do, and maps that had no baked light stay fully bright, with a sun added so weapons and players are lit too. Light probe volumes are added for the whole map. |
| Displacements | Blend displacements keep their texture blending. |
| Effects | The map's particle systems are converted, and sprites, glows, beams and lasers, sparks, tesla arcs, smoke stacks, smoke volumes, spotlights, dust, embers and fire become CS2 particle effects. Ropes become cables that sag like they did in Source 1. |
| Sky | 2D skyboxes and skies drawn from cube textures become a CS2 sky, with sky brushes that show it. The 3D skybox becomes its own skybox map, and the map's sun is drawn into the sky. |
| Post processing | The map's color correction becomes CS2 post processing, and water gets an underwater look that follows the shape of the water. |
| Sound | Ambient sounds and soundscapes become CS2 sound events with the same range and fade. |
| Map fixes | Decals are put back onto their surfaces, clip brushes are rebuilt from the `.bsp`, entities CS2 does not have are converted or removed, and unknown keys are cleaned up. Thin boost and gravity triggers are made thick enough to work, spawn points stuck in the floor are lifted, empty brush entities are removed, and entity names like `1.01` keep their exact text. |
| Missing assets | Lists what a ported map (and the models in its addon) still misses, searches for it in your games and adds it to the addon. A model whose material exists nowhere can get a placeholder material, so it still compiles. |
| Addons | Lists your CS2 addons with their maps, size and last change; opens or deletes them (to the Recycle Bin). |
| Tools | Converters for single files and folders: materials, models, images, translucency masks, skyboxes (from six faces or one open cube image), and Polyhaven / Megascans models, plants and textures. |
| Report | Every port writes a full `report.txt`: each step, every file and where it came from, and what could not be found. |
| Comfort | English, Turkish, Russian and Chinese, dark and light theme, drag and drop and copy / paste everywhere, and updates from inside the program. |

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

1. **Port Map** tab: add one or more `.bsp` files, or drag them (or a folder or archive) onto
   the list.
2. Select a map and pick the addon it goes into, or type a new addon name.
3. Check **Port settings** if you want to change how the maps are ported.
4. Press **Port**. Finished maps turn green.
5. Open the addon in Hammer (CS2 Workshop Tools), open the map and compile it.

Other tabs:

- **Missing Assets**: lists what a ported map still misses, finds it in your games and adds it
  to the addon.
- **Search Files**: finds any file by its name in your games, in CS2 or in a `.bsp`, and exports
  it with the files it needs.
- **Tools**: converts single files or folders (materials, models, images, skyboxes, Polyhaven and
  Megascans assets).
- **Addons**: lists your CS2 addons, opens their folders or deletes them.
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

Two things go online, each with plain HTTPS requests:

- At start, the program asks GitHub whether a newer release exists. Nothing is downloaded unless
  you agree to the update. Without a connection the check is skipped silently.
- When the "bhop script" option is on and the map needs it, the program downloads the newest
  version of the bhop script and keeps a copy. Without a connection the saved copy, or the copy
  built into the program, is used. Turn off "Download the newest bhop script" in Settings to
  skip this.

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

## Support

If CS2 Porter helped you, you can support it by
[sending an item on Steam](https://steamcommunity.com/tradeoffer/new/?partner=1538890593&token=F7uoB0t0)
or through [GitHub Sponsors](https://github.com/sponsors/bcanusar).

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
