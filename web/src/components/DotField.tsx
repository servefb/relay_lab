import { useEffect, useRef } from 'react';

const GAP = 20;
const BASE_RADIUS = 1;
const RADIUS = 140;
const PUSH = 14;
const GROW = 1.6;
const EASE = 0.14;

interface Dot {
  x: number;
  y: number;
  /** Current displacement and emphasis, eased towards the cursor-driven target each frame. */
  dx: number;
  dy: number;
  k: number;
}

/**
 * Full-screen dotted background: dots near the pointer swell, darken and drift away from it,
 * then ease back once the pointer leaves. The loop sleeps whenever everything has settled.
 */
export function DotField() {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const context = canvas?.getContext('2d');
    if (!canvas || !context) return;

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const pointer = { x: -9999, y: -9999 };
    let dots: Dot[] = [];
    let frame = 0;

    const resize = () => {
      const ratio = window.devicePixelRatio || 1;
      const width = window.innerWidth;
      const height = window.innerHeight;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      dots = [];
      for (let y = GAP / 2; y < height + GAP; y += GAP) {
        for (let x = GAP / 2; x < width + GAP; x += GAP) dots.push({ x, y, dx: 0, dy: 0, k: 0 });
      }
      draw(false);
    };

    /** Advance one eased step (unless `step` is false) and paint; returns whether anything still moves. */
    const draw = (step = true): boolean => {
      context.clearRect(0, 0, canvas.width, canvas.height);
      let moving = false;
      for (const dot of dots) {
        if (step) {
          const vx = dot.x - pointer.x;
          const vy = dot.y - pointer.y;
          const distance = Math.hypot(vx, vy);
          const strength = distance < RADIUS ? (1 - distance / RADIUS) ** 2 : 0;
          const tx = distance ? (vx / distance) * PUSH * strength : 0;
          const ty = distance ? (vy / distance) * PUSH * strength : 0;
          dot.dx += (tx - dot.dx) * EASE;
          dot.dy += (ty - dot.dy) * EASE;
          dot.k += (strength - dot.k) * EASE;
          if (Math.abs(tx - dot.dx) > 0.05 || Math.abs(ty - dot.dy) > 0.05 || Math.abs(strength - dot.k) > 0.005) moving = true;
        }
        const alpha = 0.1 + dot.k * 0.55;
        context.fillStyle = `rgba(20, 20, 20, ${alpha.toFixed(3)})`;
        context.beginPath();
        context.arc(dot.x + dot.dx, dot.y + dot.dy, BASE_RADIUS + dot.k * GROW, 0, Math.PI * 2);
        context.fill();
      }
      return moving;
    };

    const loop = () => {
      frame = draw() ? requestAnimationFrame(loop) : 0;
    };
    const wake = () => {
      if (!frame) frame = requestAnimationFrame(loop);
    };
    const onMove = (event: PointerEvent) => {
      pointer.x = event.clientX;
      pointer.y = event.clientY;
      wake();
    };
    const onLeave = () => {
      pointer.x = -9999;
      pointer.y = -9999;
      wake();
    };

    resize();
    window.addEventListener('resize', resize);
    if (!reduced) {
      window.addEventListener('pointermove', onMove, { passive: true });
      document.documentElement.addEventListener('pointerleave', onLeave);
      window.addEventListener('blur', onLeave);
    }
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener('resize', resize);
      window.removeEventListener('pointermove', onMove);
      document.documentElement.removeEventListener('pointerleave', onLeave);
      window.removeEventListener('blur', onLeave);
    };
  }, []);

  return <canvas ref={canvasRef} className="dot-field" aria-hidden="true" />;
}
