# APRS symbol sprites

The aprs.fi symbol set by Heikki Hannikainen, OH7LZB: https://github.com/hessu/aprs-symbols/
(the `png/aprs-symbols-24-N@2x.png` files, renamed here to say they hold 48x48 px symbols).
Free for use in APRS applications. Per-symbol sources and licenses are in that
repo's COPYRIGHT.md.

- `aprs-symbols-48-0.png`: primary table (`/`)
- `aprs-symbols-48-1.png`: alternate table (`\`)
- `aprs-symbols-48-2.png`: overlay characters (0-9, A-Z drawn over an alternate-table symbol)

Each sheet is 16 columns x 6 rows of 48x48 px cells. Symbol code `c` (ASCII 33-126)
sits at index `ord(c) - 33`, so column `index % 16` and row `index // 16`.
