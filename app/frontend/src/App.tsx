import { NavLink, Route, Routes, Link } from "react-router-dom";
import TaskListPage from "./pages/TaskListPage";
import CreateTaskPage from "./pages/CreateTaskPage";
import TaskDetailPage from "./pages/TaskDetailPage";

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-dot" /> agent-workspace
        </Link>
        <nav>
          <NavLink to="/" end>
            Tasks
          </NavLink>
          <NavLink to="/new">New Task</NavLink>
        </nav>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<TaskListPage />} />
          <Route path="/new" element={<CreateTaskPage />} />
          <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
        </Routes>
      </main>
    </div>
  );
}
