# Third-party notices

## Fleasion

Fleasion is an open-source Roblox asset inspection and replacement project: https://github.com/fleasion/Fleasion
The upstream repository is licensed under the GNU General Public License v3.0 (GPL-3.0).

CacheSwap v1.7 adapts source files from user-provided Fleasion source archives. The adapted modules
include the GUI theme manager and file-drop helper, the asset-type filter, JSON tree viewer, font
viewer, mesh-to-OBJ converter, RBXM binary parser and RBXM/RBXMX preview widget. PyQt6 calls were
adapted to PySide6 in these modules, and the RBXM binary preview uses a small adapter to the included
parser rather than importing Fleasion's complete solid-model deserializer stack. Some additional
Fleasion GUI modules were not integrated because their required proxy, configuration and cache-management
subsystems were not included in the supplied source files.

Copyright in the original Fleasion code remains with its respective contributors. These adapted
components and CacheSwap are distributed under GPL-3.0. Keep this notice and the `LICENSE` file with
redistributions. See the upstream repository for the complete authorship and licensing information.
