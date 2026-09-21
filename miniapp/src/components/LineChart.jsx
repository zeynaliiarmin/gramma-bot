// A lightweight dependency-free SVG line chart (Persian-friendly).

export default function LineChart({ title, labels = [], values = [], color = 'rgba(91,75,255,0.9)' }) {
  const w = 520
  const h = 160
  const pad = 10
  if (!labels.length || !values.length) {
    return (
      <div className="empty">داده‌ای برای نمایش وجود ندارد</div>
    )
  }
  const max = Math.max(...values, 1)
  const stepX = (w - pad * 2) / (labels.length - 1 || 1)
  const pts = values.map((v, i) => [
    pad + i * stepX,
    h - pad - (v / max) * (h - pad * 2),
  ])
  const line = pts.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${h - pad} L${pts[0][0].toFixed(1)},${h - pad} Z`

  const gradId = 'g' + title.replace(/\W/g, '')
  return (
    <div className="card">
      <h3>{title}</h3>
      <svg viewBox={`0 0 ${w} ${h}`} className="chart" style={{ direction: 'ltr' }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.28" />
            <stop offset="100%" stopColor={color} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gradId})`} />
        <path d={line} fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" />
        {pts.map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r="3" fill="#fff" stroke={color} strokeWidth="2" />
        ))}
        {labels.map((lb, i) => (
          <text key={i} x={pad + i * stepX} y={h - 1} fontSize="9" textAnchor="middle" fill="#9aa0b4">
            {lb}
          </text>
        ))}
      </svg>
    </div>
  )
}
