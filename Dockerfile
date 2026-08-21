FROM docker-repo.test.com/python:3.11.11-slim
WORKDIR /app
COPY . /app


RUN pip install --no-cache-dir -U pip
RUN pip install --no-cache-dir --index-url http://nexus.test.com:8081/repository/pypi-group/simple fastmcp
RUN pip install --no-cache-dir --index-url http://nexus.test.com:8081/repository/pypi-group/simple -r requirements.txt

# 패키지라 -m 으로 띄운다. WORKDIR 이 /app 이라 sys.path 에 /app 이 들어가고
# src 패키지가 잡힌다. "python src/main.py" 는 /app/src 만 path 에 넣어 import 가 깨진다.
CMD ["python", "-m", "src"]
