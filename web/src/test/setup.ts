import { vi } from 'vitest'
import '@testing-library/jest-dom/vitest'

// jsdom doesn't implement canvas — mock getContext to suppress warnings
HTMLCanvasElement.prototype.getContext = vi.fn().mockReturnValue(null)
