# Third party software and trademarks

The release zip of CS2 Porter comes with the programs below in its `tools` folder. They are
separate programs that CS2 Porter starts; they are included unchanged, as published by their
authors, and given away free of charge. Their license texts are in the `licenses` folder.

## BSPSource 1.4.8 (`tools/BSPSource`)

Opens `.bsp` maps. https://github.com/ata4/bspsrc (release v1.4.8)

| Part | License | Text |
|---|---|---|
| BSPSource, ioutils | Unlicense (public domain) | `licenses/BSPSource-Unlicense.txt`, `licenses/ioutils-Unlicense.txt` |
| Apache Log4j 2, Commons Compress / IO / Lang / Codec, picocli, FlatLaf | Apache License 2.0 | `licenses/Apache-2.0.txt` |
| MigLayout | BSD 3-Clause | `licenses/MigLayout-BSD-3-Clause.txt` |
| JSVG | MIT | `licenses/jsvg-MIT.txt` |
| XZ for Java | 0BSD | `licenses/XZ-for-Java-0BSD.txt` |
| Java runtime (OpenJDK 24.0.2, part of the BSPSource Windows release) | GPL-2.0 with the Classpath Exception | `tools/BSPSource/legal/` |

Source code: BSPSource https://github.com/ata4/bspsrc, OpenJDK https://github.com/openjdk/jdk24u.

## VTFCmd Reloaded 2.0.0 (`tools/VTFCmd`, optional)

Reads the few VTF texture formats CS2 Porter can not read by itself.

| Part | License | Text |
|---|---|---|
| VTFCmd.exe | GPL-2.0 | `licenses/VTFCmd-GPL-2.0.txt` |
| VTFLib.dll | LGPL-2.1 | `licenses/VTFLib-LGPL-2.1.txt` |
| DevIL.dll 1.8.0 | LGPL-2.1 | `licenses/DevIL-LGPL-2.1.txt` |

Source code: VTFCmd and VTFLib https://github.com/misyltoad/VTFLib, DevIL
https://github.com/DentonW/DevIL. Copyright (C) 2005-2011 Neil Jedrzejewski & Ryan Gregg,
(C) 2020 Joshua Ashton.

## Not included

The Counter-Strike 2 Workshop Tools (`source1import`, `resourcecompiler`, `cs_mdl_import`,
`dmxconvert`) are part of Counter-Strike 2. CS2 Porter runs them from your own CS2 install.

## Python packages (inside CS2 Porter.exe)

| Package | License |
|---|---|
| Pillow | MIT-CMU (HPND) |
| NumPy | BSD-3-Clause |
| PyInstaller bootloader | GPL-2.0 with an exception that allows any license for the built program |

## Trademarks

Counter-Strike, Counter-Strike 2, Source, Steam and Hammer are trademarks of Valve Corporation.
CS2 Porter is a fan made tool. It is not made, endorsed or supported by Valve.
