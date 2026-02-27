from veracity import create_app, db

app = create_app()

with app.app_context():
    print("Dropping all tables...")
    db.drop_all()  
    
    print("Creating all tables...")
    db.create_all() 
    
    print("Done! Database is fresh.")