// Motion never delays state updates: interrupted transitions keep the latest UI.
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const active = new Map();
const easing = 'cubic-bezier(.22, 1, .36, 1)';

function stop(node) {
  active.get(node)?.cancel();
  active.delete(node);
}

function animate(node, frames, duration = 260) {
  stop(node);
  if (reducedMotion.matches || !node.animate || !node.getClientRects().length) return;
  const animation = node.animate(frames, {duration, easing});
  active.set(node, animation);
  const cleanup = () => { if (active.get(node) === animation) active.delete(node); };
  animation.onfinish = cleanup;
  animation.oncancel = cleanup;
}

reducedMotion.addEventListener('change', () => {
  if (reducedMotion.matches) for (const node of active.keys()) stop(node);
});

export function reveal(node, distance = 12) {
  animate(node, [
    {opacity: 0, transform: `translateY(${distance}px)`},
    {opacity: 1, transform: 'translateY(0)'}
  ]);
}

export function setVisible(node, visible) {
  if (node.classList.contains('hidden') === !visible) return;
  stop(node);
  node.classList.toggle('hidden', !visible);
  if (visible) reveal(node, 6);
}

// FLIP: retain DOM nodes and drafts while cards move into their new positions.
export function layoutChange(container, update) {
  const before = new Map();
  for (const node of container.children) {
    if (node.getClientRects().length) before.set(node, node.getBoundingClientRect());
    stop(node);
  }
  update();
  for (const node of container.children) {
    if (!node.getClientRects().length) continue;
    const old = before.get(node), next = node.getBoundingClientRect();
    if (!old) { reveal(node); continue; }
    const x = old.left - next.left, y = old.top - next.top;
    if (Math.abs(x) > .5 || Math.abs(y) > .5) {
      animate(node, [{transform: `translate(${x}px, ${y}px)`}, {transform: 'translate(0, 0)'}]);
    }
  }
}

// The hidden state is committed immediately; the wrapper animates only its size.
export function toggleDisclosure(node, visible) {
  if (node.classList.contains('hidden') === !visible) return;
  const parent = node.parentElement;
  const height = parent.getBoundingClientRect().height;
  stop(parent);
  setVisible(node, visible);
  const next = parent.getBoundingClientRect().height;
  if (height !== next) animate(parent, [{height: `${height}px`, overflow: 'clip'}, {height: `${next}px`, overflow: 'clip'}]);
}

export function scrollToNode(node) {
  node.scrollIntoView({block: 'nearest', behavior: reducedMotion.matches ? 'instant' : 'smooth'});
}
