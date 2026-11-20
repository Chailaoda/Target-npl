import type { ReactNode } from 'react'

export default function Card({ step, title, desc, aside, children, className = '' }: {
  step?: string
  title: string
  desc?: string
  aside?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`card ${className}`}>
      <div className="card-head">
        <div>
          <div className="card-title">{step && <span className="step">{step}</span>}<h2>{title}</h2></div>
          {desc && <p className="card-desc">{desc}</p>}
        </div>
        {aside && <div className="card-aside">{aside}</div>}
      </div>
      <div className="card-body">{children}</div>
    </section>
  )
}
