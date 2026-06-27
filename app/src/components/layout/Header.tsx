import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface HeaderProps {
  onConnectDatabase: () => void;
  onUploadSchema: () => void;
}

const Header = ({ onConnectDatabase, onUploadSchema }: HeaderProps) => {
  return (
    <div className="flex items-center justify-between p-6 border-b border-border">
      <div className="flex items-center space-x-4">
        {/* Database Selector */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" className="min-w-[150px] justify-between">
              Select Database
              <ChevronDown className="w-4 h-4 opacity-50" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-[200px]">
            <DropdownMenuItem disabled className="text-muted-foreground">
              No databases connected
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        {/* Upload Schema */}
        <div className="relative">
          <Button 
            variant="outline" 
            className="min-w-[140px] justify-between" 
            title="Upload YAML schema metadata"
            onClick={onUploadSchema}
          >
            Upload Schema
            <ChevronDown className="w-4 h-4 opacity-50" />
          </Button>
        </div>

        {/* Connect Database Button */}
        <Button onClick={onConnectDatabase} className="bg-primary hover:bg-primary-dark">
          Connect Database
        </Button>
      </div>
    </div>
  );
};

export default Header;
