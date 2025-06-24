# Dockerfile

# 1. Use an official Python runtime as a parent image
FROM python:3.9-slim

# 2. Set the working directory in the container
WORKDIR /usr/src/app

# 3. Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV LANG C.UTF-8

# 4. Copy ALL application files into the container first.
# This includes main.py, requirements.txt, babel.cfg, the 'translations' directory, etc.
COPY . .

# 5. Now that requirements.txt is inside the container, install dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# 6. Now that babel.cfg and the source code are present, run the Babel commands.
# This chain will now work because babel.cfg exists in the current directory (`.`)
# RUN pybabel compile -d translations

# 7. Expose the port your application will run on
EXPOSE 80

# 8. Set the production-ready run command
CMD ["gunicorn", "main:app", "-b", "0.0.0.0:80"]