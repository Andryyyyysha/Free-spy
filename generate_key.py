from cryptography.fernet import Fernet

key = Fernet.generate_key().decode()
print("Ваш секретный ключ шифрования (ENCRYPTION_KEY):")
print("-" * 50)
print(key)
print("-" * 50)
print("Добавьте этот ключ в переменные окружения на Render под именем ENCRYPTION_KEY.")
