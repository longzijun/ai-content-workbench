import ReactDOM from 'react-dom/client';
import App from './App';
import { registerWorkbenchApp } from './pwa';
import './style.css';
// 主题层放在 style.css 之后：同特异度下靠后的规则生效，保证蓝灰 SaaS
// 令牌覆盖暖绿旧值，而 style.css 里未被覆盖的选择器照常工作。
import './saas.css';
import './upgrade.css';
import './ios.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <App />,
);
registerWorkbenchApp();
