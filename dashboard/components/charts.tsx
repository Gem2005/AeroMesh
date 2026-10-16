export interface Series {
  values: number[];
  color: string;
  fill?: boolean;
}

/** Right-aligned rolling line chart rendered as a scalable SVG. */
export function LineChart({
  series,
  min,
  max,
  slots = 60,
  gridLines = [0.25, 0.5, 0.75],
}: {
  series: Series[];
  min: number;
  max: number;
  slots?: number;
  gridLines?: number[];
}) {
  const width = slots - 1;
  const span = max - min || 1;
  const toY = (v: number) => 100 - ((Math.min(max, Math.max(min, v)) - min) / span) * 100;

  return (
    <svg viewBox={`0 0 ${width} 100`} preserveAspectRatio="none" className="h-full w-full">
      {gridLines.map((g) => (
        <line
          key={g}
          x1={0}
          x2={width}
          y1={g * 100}
          y2={g * 100}
          stroke="#1e293b"
          strokeDasharray="2 3"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {series.map((s, si) => {
        if (s.values.length === 0) return null;
        const offset = width - (s.values.length - 1);
        const pts = s.values.map((v, i) => `${offset + i},${toY(v)}`);
        const line = pts.join(" ");
        return (
          <g key={si}>
            {s.fill && s.values.length > 1 && (
              <polygon
                points={`${offset},100 ${line} ${width},100`}
                fill={s.color}
                fillOpacity={0.12}
              />
            )}
            <polyline
              points={line}
              fill="none"
              stroke={s.color}
              strokeWidth={1.6}
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </g>
        );
      })}
    </svg>
  );
}
