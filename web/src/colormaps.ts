// Ugyanazok a színtérképek, mint a szerveren (src/satrx/dsp/spectrum.py), hogy az élő és az utólagos waterfall egyezzen.
const STOPS: Record<string, [number, number, number][]> = {
  viridis: [[68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142], [31, 158, 137], [53, 183, 121], [110, 206, 88], [181, 222, 43], [253, 231, 37]],
  heat: [[0, 0, 0], [120, 0, 0], [230, 40, 0], [255, 170, 0], [255, 255, 120], [255, 255, 255]],
  jet: [[0, 0, 128], [0, 0, 255], [0, 255, 255], [255, 255, 0], [255, 0, 0], [128, 0, 0]],
  gray: [[0, 0, 0], [255, 255, 255]],
};
export const COLORMAP_NAMES = Object.keys(STOPS);

/** 256 x 3 értékű keresőtábla (R, G, B egymás után). */
export function colormapLut(name: string): Uint8ClampedArray {
  const stops = STOPS[name] ?? STOPS.viridis;
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const pos = (i / 255) * (stops.length - 1);
    const k = Math.min(stops.length - 2, Math.floor(pos));
    const f = pos - k;
    for (let c = 0; c < 3; c++) lut[i * 3 + c] = Math.round(stops[k][c] * (1 - f) + stops[k + 1][c] * f);
  }
  return lut;
}
