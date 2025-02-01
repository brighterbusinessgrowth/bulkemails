// Add interactivity here
document.getElementById('campaignForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = {
        emails: document.getElementById('emails').value,
        subject: document.getElementById('subject').value,
        body: document.getElementById('body').value
    };

    try {
        const response = await fetch('/send-emails', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        });
        
        const data = await response.json();
        if(data.status === 'success') {
            alert(`Successfully sent ${data.sent} emails!`);
        }
    } catch (error) {
        console.error('Error:', error);
    }
});