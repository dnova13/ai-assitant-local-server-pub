import subprocess
import psutil
import json
import re
import ollama

# -------------------------------------------------------------------
# 1. 안전 실행 쉘 명령어 화이트리스트
# -------------------------------------------------------------------
ALLOWED_COMMANDS = {
    "disk_usage": ["df", "-h"],
    "mem_usage": ["free", "-h"],
    "uptime": ["uptime", "-p"],
    "docker_ps": ["docker", "ps"],
}

# -------------------------------------------------------------------
# 2. 시스템 도구(Tools) 함수 정의
# -------------------------------------------------------------------
def get_system_status() -> str:
    """CPU 사용률, 메모리, 루트 디스크 잔여 용량을 반환합니다."""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    return (
        f"[시스템 상태 수집 결과]\n"
        f"- CPU: {cpu}%\n"
        f"- RAM: {mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB ({mem.percent}%)\n"
        f"- Disk: {disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB ({disk.percent}%)"
    )

def execute_safe_command(action_key: str) -> str:
    """사전 정의된 쉘 명령어를 실행합니다. (옵션: disk_usage, mem_usage, uptime, docker_ps)"""
    if action_key not in ALLOWED_COMMANDS:
        return f"실행 거부: '{action_key}'는 허용되지 않은 명령어입니다."
    try:
        res = subprocess.run(ALLOWED_COMMANDS[action_key], capture_output=True, text=True, check=True, timeout=10)
        return res.stdout.strip()
    except Exception as e:
        return f"명령 실행 실패: {str(e)}"

def manage_service(service_name: str, action: str) -> str:
    """특정 서비스(nginx, docker, mariadb, mysql, redis-server)의 상태를 제어/조회합니다."""
    allowed_services = ["nginx", "docker", "mariadb", "mysql", "redis-server"]
    allowed_actions = ["status", "restart", "start", "stop"]

    if service_name not in allowed_services or action not in allowed_actions:
        return f"제어 거부: {service_name} 또는 {action} 비허가."

    cmd = ["sudo", "systemctl", action, service_name] if action != "status" else ["systemctl", "status", service_name]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.stdout.strip() or f"{service_name} {action} 완료"
    except Exception as e:
        return f"서비스 제어 실패: {str(e)}"

AVAILABLE_TOOLS = {
    "get_system_status": get_system_status,
    "execute_safe_command": execute_safe_command,
    "manage_service": manage_service,
}

# -------------------------------------------------------------------
# 3. 에이전트 실행 엔진 (텍스트 JSON 폴백 처리 포함)
# -------------------------------------------------------------------
def chat_with_agent(prompt: str):
    print(f"\n[User]: {prompt}")
    
    messages = [
        {
            "role": "system",
            "content": "당신은 Ubuntu 24.04 서버 관리 AI 비서입니다. 시스템 상태 조회나 제어 요청이 오면 반드시 제공된 도구를 호출하세요."
        },
        {"role": "user", "content": prompt}
    ]

    response = ollama.chat(
        model="qwen2.5-coder:3b",
        messages=messages,
        tools=[get_system_status, execute_safe_command, manage_service],
        options={"temperature": 0.1, "num_ctx": 4096}
    )

    tool_calls = response.message.tool_calls or []
    
    # [방어 로직] tool_calls 객체가 비어있고 텍스트로 JSON이 온 경우 파싱
    if not tool_calls and response.message.content:
        content = response.message.content.strip()
        match = re.search(r'\{.*"name"\s*:\s*"(\w+)".*\}', content, re.DOTALL)
        if match:
            try:
                parsed_json = json.loads(match.group(0))
                func_name = parsed_json.get("name")
                func_args = parsed_json.get("arguments", {})
                if func_name in AVAILABLE_TOOLS:
                    print(f"⚙️ [Tool 감지 및 직접 실행]: {func_name}({func_args})")
                    tool_output = AVAILABLE_TOOLS[func_name](**func_args)
                    
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": f"도구 실행 결과:\n{tool_output}\n위 결과를 바탕으로 사용자에게 자연어로 요약 보고해줘."})
                    
                    final_response = ollama.chat(model="qwen2.5-coder:3b", messages=messages)
                    print(f"\n[Assistant]:\n{final_response.message.content}")
                    return
            except Exception:
                pass

    # 정규 Function Calling 경로
    if tool_calls:
        messages.append(response.message)
        for tool_call in tool_calls:
            func_name = tool_call.function.name
            func_args = tool_call.function.arguments
            
            if func_name in AVAILABLE_TOOLS:
                print(f"⚙️ [Tool 실행]: {func_name}({func_args})")
                tool_output = AVAILABLE_TOOLS[func_name](**func_args)
                messages.append({"role": "tool", "content": str(tool_output)})
        
        final_response = ollama.chat(model="qwen2.5-coder:3b", messages=messages)
        print(f"\n[Assistant]:\n{final_response.message.content}")
    else:
        print(f"\n[Assistant]:\n{response.message.content}")

if __name__ == "__main__":
    print("🤖 서버 AI 비서가 준비되었습니다. (종료하려면 'exit' 또는 'quit' 입력)\n")
    while True:
        try:
            user_input = input("\n[나]: ")
            if user_input.strip().lower() in ["exit", "quit", "q"]:
                print("비서를 종료합니다.")
                break
            if not user_input.strip():
                continue
            chat_with_agent(user_input)
        except KeyboardInterrupt:
            print("\n비서를 종료합니다.")
            break