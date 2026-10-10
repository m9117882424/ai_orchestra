# Repo Manager: SSH deploy key (GitHub, read-only)

## Транспорт

Registry по-прежнему хранит канонический HTTPS remote без credential.
Секретный профиль Repo Manager выбирает SSH для двух Git сетевых операций:
`ls-remote` и `fetch`. Bare mirror хранит канонический HTTPS origin.
Другие подсистемы, включая LLM, не получают ключ, SSH agent или сетевой Git-доступ.

Пример в `.env.repositories` (файл 0600, вне Git):

```dotenv
REPO_MANAGER_AUTH_PROFILES_JSON='{"git-readonly-primary":{"host":"github.com","transport":"ssh","identity_file":"/run/secrets/taxi-deploy-key","known_hosts_file":"/run/secrets/github-known-hosts"}}'
```

Пример Docker Compose override: `deploy/docker-compose.repo-manager-ssh.override.example.yml`.
Установить как `/opt/ai_orchestra/docker-compose.override.yml` (игнорируется Git).
Обратите внимание: Docker Compose bind-mounted secrets сохраняют владение и права host-файла.
`repo-manager` работает с UID/GID 10001:10001, поэтому ключ должен быть
отдельным deploy key (не основным ключом root), принадлежать 10001:10001, mode 0400.
Каталог host `/etc/ai-orchestra/repo-keys` должен оставаться root-owned с mode 0700.
`known_hosts` может быть root-owned mode 0444, mount read-only. Никаких ключей
в исходниках или image build context.

GitHub deploy key добавить в Settings → Deploy keys конкретного репозитория,
не разрешая write access. Проверить fingerprint публичного ключа с
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
Используется GitHub ED25519 fingerprint:
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`.

Защита транспорта:
- DNS remote revalidate с запретом private/reserved адресов, pin выбранного IP,
  SSH host key alias `github.com`, обязательный заранее проверенный `known_hosts`;
- `StrictHostKeyChecking=yes`, отключены пользовательские конфиги, agent,
  proxy/jump, remote forward, interactive password и смена host keys;
- разрешён только `git-upload-pack` на `github.com`, никакого push;
- нет credential в Registry, Git URL, stdout или audit; classified failure,
  fenced lease и mirror fsck работают как прежде.

## Проверка

1. Запустить тесты, собрать repo-manager image, сверить Compose и смонтированные
   секреты на disposable экземпляре, убедиться в правах только на чтение.
2. Из Control Plane сделать `GET /api/repositories/{repository_id}`, прочитать
   `version`, вызвать штатный `POST /api/repositories/{repository_id}/validate`
   с `{"expected_version": <полученная версия>}` и заголовком `X-Control-Request: ai-orchestra`.
3. Дождаться `ready`, проверить `default_branch` и `last_known_commit` против
   GitHub HEAD, `last_sync_error_code=null`, `sync_failure_count=0`.
4. Проверить повторный refresh, `fsck`, audit без секретов и сохранение
   `ai-orchestra-acceptance=ready`.
5. При сбое вернуть предыдущий repo-manager image и Compose override;
   Registry не подменять на `ready` вручную.

SSH-ключ не монтируется в control-plane, workspace-manager или OpenCode.
Production Corporate Taxi не требуется перезапускать и не следует затрагивать.
