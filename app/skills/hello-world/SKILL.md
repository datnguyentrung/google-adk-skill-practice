---
name: hello-world
description: Tạo lời chào Hello World thân thiện. Dùng skill này khi người dùng yêu cầu nói xin chào, viết Hello World, chào một người cụ thể hoặc kiểm thử agent chào hỏi.
---

# Hello World Skill

Khi skill này được kích hoạt, thực hiện đúng các bước sau:

1. Dùng `load_skill_resource` để đọc file `references/greeting-template.md`.
2. Xác định tên người cần chào từ yêu cầu của người dùng.
3. Nếu người dùng không cung cấp tên, dùng từ `bạn`.
4. Thay `{name}` trong template bằng tên đã xác định.
5. Trả về đúng một câu chào hoàn chỉnh.
6. Không mô tả các bước xử lý và không nhắc đến tên tool trong câu trả lời.
