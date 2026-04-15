import asyncio
import time
import uuid
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

@dataclass
class InferenceTask:
    id: str
    model: str
    messages: List[Dict[str, str]]
    temperature: float = 0.7
    stream: bool = False
    status: str = "queued"  # queued | running | completed | cancelled | failed
    response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    start_time: float = 0
    end_time: float = 0
    latency_ms: float = 0
    
    # Synchronization
    is_ready: asyncio.Event = field(default_factory=asyncio.Event)
    is_done: asyncio.Event = field(default_factory=asyncio.Event)
    future: asyncio.Future = field(default_factory=lambda: asyncio.Future())

class ExecutionController:
    def __init__(self, max_queue_size: int = 50):
        self._queue = asyncio.Queue(maxsize=max_queue_size)
        self._states: Dict[str, InferenceTask] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._worker_task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background worker."""
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
            log.info("Execution controller worker started.")

    async def _worker(self):
        """Sequential global execution worker that gives 'turns' to requests."""
        while True:
            task: InferenceTask = await self._queue.get()
            
            if task.status == "cancelled":
                self._queue.task_done()
                continue

            task.status = "running"
            task.start_time = time.monotonic()
            
            # 1. Signal the request handler that it's their turn
            task.is_ready.set()
            
            # 2. Track this task in case of cancellation while running
            # Note: actual execution happens in main.py, but we wait here for completion
            try:
                # Wait for the request handler to signal 'is_done'
                # or for the task to be cancelled via self.cancel_request
                await task.is_done.wait()
                if task.status != "cancelled":
                    task.status = "completed"
            except asyncio.CancelledError:
                task.status = "cancelled"
                task.error = "Request cancelled via worker."
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
            finally:
                task.end_time = time.monotonic()
                task.latency_ms = (task.end_time - task.start_time) * 1000
                self._active_tasks.pop(task.id, None)
                self._queue.task_done()

    def get_status(self, request_id: str) -> Optional[InferenceTask]:
        """Retrieve status of a request."""
        return self._states.get(request_id)
        
    async def add_request(self, model: str, messages: List[Dict[str, str]], temperature: float = 0.7, stream: bool = False) -> InferenceTask:
        """Add a new request to the queue."""
        if self._queue.full():
            raise RuntimeError("server_busy")

        req_id = f"req-{uuid.uuid4().hex[:8]}"
        task = InferenceTask(id=req_id, model=model, messages=messages, temperature=temperature, stream=stream)
        self._states[req_id] = task
        
        await self._queue.put(task)
        return task

    def register_running_task(self, request_id: str, task: asyncio.Task):
        """Link a running asyncio task to a request ID for cancellation support."""
        self._active_tasks[request_id] = task

    async def cancel_request(self, request_id: str) -> bool:
        """Cancel a queued or running request."""
        task = self._states.get(request_id)
        if not task:
            return False

        if task.status in ["completed", "failed", "cancelled"]:
            return False

        # 1. Cancel the actual asyncio task if it's been registered
        exec_task = self._active_tasks.get(request_id)
        if exec_task and not exec_task.done():
            exec_task.cancel()
        
        # 2. Update state
        task.status = "cancelled"
        task.error = "Cancelled by user."
        
        # 3. Signal completion to worker so it can move on
        if not task.is_done.is_set():
            task.is_done.set()
            
        return True

# Global singleton
controller = ExecutionController()
